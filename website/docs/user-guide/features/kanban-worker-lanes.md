# Kanban worker lanes

A **worker lane** is a class of process that the kanban dispatcher can route tasks to. Each lane has an identity (the assignee string), a spawn mechanism, and a contract for what it must do with the task once spawned.

This page is the contract. It exists for two audiences:

- **Operators** picking which lanes to wire into a board (which profiles to create, which assignees to use).
- **Plugin / integration authors** wanting to add a new lane shape (a CLI worker that wraps Codex / Claude Code / OpenCode, a containerised review worker, a non-Hermes service that pulls tasks via the API).

If you're writing the worker code itself — the agent that runs *inside* a lane — the [`kanban-worker`](https://github.com/NousResearch/hermes-agent/blob/main/skills/devops/kanban-worker/SKILL.md) skill is the deeper procedural detail.

## The hierarchy

```text
Hermes Kanban  =  canonical task lifecycle + audit trail
Worker lane    =  implementation executor for one assigned card
Reviewer       =  human or human-proxy that gates "done"
GitHub PR      =  upstreamable artifact (optional, for code lanes)
```

Hermes Kanban owns lifecycle truth — `ready` → `running` → `blocked` / `done` / `archived`. Worker lanes execute work but never own that truth; everything they do flows back through the kanban kernel via the `kanban_*` tools (or, for non-Hermes external workers, via the API). Reviewers gate the transition from "code change written" to "task done."

## What a lane provides

To be a kanban worker lane, an integration must provide three things:

### 1. An assignee string

The dispatcher resolves `task.assignee` in this order:

1. registered external worker lane
2. Hermes profile name
3. `skipped_nonspawnable`

This keeps existing profile workers compatible while allowing names such as `codex-fast`, `codex-deep`, and `codex-review` to be registered as external lanes. Unknown terminal/control-plane names still stay in `ready` or `review` and appear in `skipped_nonspawnable` rather than being spawned through a broken fallback.

### 2. A spawn mechanism

For Hermes profile lanes, the dispatcher's `_default_spawn` runs `hermes -p <assignee> chat -q <prompt>` (or the equivalent module form when the `hermes` shim isn't on `$PATH`) inside the task's pinned workspace, with these env vars set:

| Variable | Carries |
|---|---|
| `HERMES_KANBAN_TASK` | the task id the worker is operating on |
| `HERMES_KANBAN_DB` | absolute path to the per-board SQLite file |
| `HERMES_KANBAN_BOARD` | board slug |
| `HERMES_KANBAN_WORKSPACES_ROOT` | root of the board's workspace tree |
| `HERMES_KANBAN_WORKSPACE` | absolute path to *this* task's workspace |
| `HERMES_KANBAN_RUN_ID` | the current run's id (for the lifecycle gate) |
| `HERMES_KANBAN_CLAIM_LOCK` | the claim lock string (`<host>:<pid>:<uuid>`) |
| `HERMES_PROFILE` | the worker's own profile name (for `kanban_comment` author attribution) |
| `HERMES_TENANT` | tenant namespace, if the task has one |

For non-Hermes lanes, the worker lane registry supplies a trusted `spawn_fn` callable that gets `task`, `workspace`, and `board` and returns an optional pid for crash detection. Lanes can be registered from config or by plugins:

```python
def register(ctx):
    ctx.register_worker_lane(
        name="my-cli-worker",
        kind="plugin",
        description="Runs my trusted CLI worker",
        spawn_fn=spawn_my_worker,
        success_policy="block_for_review",
        max_concurrency=1,
    )
```

Plugin lane registration failures are logged and do not stop Hermes startup.

### 3. A lifecycle terminator

Every claim must end in exactly one of:

- `kanban_complete(summary=..., metadata=...)` — task succeeds, status flips to `done`.
- `kanban_block(reason=...)` — task waits for human input, status flips to `blocked`. The dispatcher respawns when `kanban_unblock` runs.
- The worker process exits without a tool call. The kernel reaps it and emits `crashed` (PID died) or `gave_up` (consecutive-failure breaker tripped) or `timed_out` (max_runtime exceeded). This is the failure path; healthy workers don't end here.

The kanban kernel enforces that exactly one of these terminates each run. A worker that calls neither and exits normally is treated as crashed.

## Outputs and the review-required convention

For most code-changing tasks, the work isn't truly *done* the moment the worker finishes — it needs independent review and verification. The kanban kernel doesn't enforce this distinction (a "code-changing task" is fuzzy and forcing block-instead-of-complete on every code worker would break flows where no review is wanted). It's a convention layered on top:

- **Block instead of complete**, with `reason` prefixed `review-required: ` so the dashboard / `hermes kanban show` surfaces the row as awaiting review.
- **Drop structured metadata into a `kanban_comment` first** since `kanban_block` only carries the human-readable `reason`. Comments are the durable annotation channel — every audit-relevant field (changed_files, tests_run, diff_path or PR url, decisions) belongs there.
- **A controller plans follow-up review/test tasks**, usually assigned to external lanes such as `codex-review` and `codex-test`, so review and verification are separate worker runs rather than Hermes reading the full implementation session.
- **Reviewer either approves the bounded evidence**, after review/test evidence is satisfactory, or asks for changes via another comment, which the next worker run sees as part of `kanban_show`'s context.

The [`kanban-worker`](https://github.com/NousResearch/hermes-agent/blob/main/skills/devops/kanban-worker/SKILL.md) skill has worked examples for both `kanban_complete` (truly terminal tasks — typo fixes, docs changes, research writeups) and the `review-required` block pattern.

## Logs and audit trail

The dispatcher writes per-task worker stdout/stderr to `<board-root>/logs/<task_id>.log`. Logs are auditable from kanban metadata:

- `task_runs` rows carry the `log_path`, exit code (where available), summary, and metadata.
- `task_events` rows carry every state transition (`promoted`, `claimed`, `heartbeat`, `completed`, `blocked`, `gave_up`, `crashed`, `timed_out`, `reclaimed`, `claim_extended`).
- `kanban_show` returns both, so a reviewer (or a follow-up worker) reading the task gets the full history without needing dashboard access.

The dashboard renders run history with summaries, metadata blocks, and exit-status badges. CLI users can run `hermes kanban tail <task_id>` to follow live, or `hermes kanban runs <task_id>` for the historical attempt list.

## Existing lane shapes

### Hermes profile lane (default)

The shape every kanban worker takes today: the assignee is a profile name, the dispatcher spawns `hermes -p <profile>`, the worker auto-loads the [`kanban-worker`](https://github.com/NousResearch/hermes-agent/blob/main/skills/devops/kanban-worker/SKILL.md) skill plus the `KANBAN_GUIDANCE` system-prompt block, and uses the `kanban_*` tools to terminate the run. No setup beyond defining the profile.

When you create profiles for your fleet, choose names that match the *role* you want the orchestrator to route to. The LLM decomposer builds its assignee roster from both `hermes profile list` and the worker lane registry; worker lanes use their lane description. There is no fixed roster the system assumes (see the [`kanban-orchestrator`](https://github.com/NousResearch/hermes-agent/blob/main/skills/devops/kanban-orchestrator/SKILL.md) skill for the orchestrator side of the contract).

### Orchestrator profile lane

A specialisation of the profile lane: an orchestrator is a Hermes profile whose toolset includes `kanban` but excludes `terminal` / `file` / `code` / `web` for implementation. Its job is decomposing a high-level goal into child tasks via `kanban_create` + `kanban_link` and stepping back. The orchestrator skill encodes the anti-temptation rules.

## Codex CLI adapter

Codex CLI is the first built-in external adapter. Configure one or more lanes in `config.yaml`:

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
      # Optional: ingest Codex CLI JSONL events into task_events.
      json_events: true

    codex-review:
      type: codex_cli
      model: gpt-5.5
      sandbox: read-only
      approval: never
      max_concurrency: 1
      success_policy: block_for_review

    codex-test:
      type: codex_cli
      model: gpt-5.4-mini
      sandbox: workspace-write
      approval: never
      max_concurrency: 1
      success_policy: block_for_review
```

The default review controller uses `codex-review` for independent review and
`codex-test` for independent verification. Configure both lanes, or pass
explicit `--review-assignee` / `--test-assignee` values to the review and
goal-advance commands. Diagnostics will warn when planned review/test follow-up
tasks reference a lane or profile that is not spawnable.

The adapter runs a Hermes-owned wrapper process, and that wrapper starts Codex with fixed argv:

```text
codex --cd <workspace> --sandbox <sandbox> --ask-for-approval <approval> [--model <model>] exec -
```

The command is not taken from model output and is not an arbitrary shell string. The wrapper passes a small allowlisted environment to Codex rather than forwarding every secret variable.
When `json_events: true` is set for a lane, the wrapper uses Codex CLI JSONL
mode instead:

```text
codex --cd <workspace> --sandbox <sandbox> --ask-for-approval <approval> [--model <model>] exec --json -
```

It records bounded `worker_codex_event` rows for Codex thread, item, command,
and usage events. Agent-message text and command summaries from those JSONL
events still feed the same progress, receipt, and metadata parsers. Leave
`json_events` unset for the legacy stdout/stderr parser path.
Progress snapshots returned by `hermes kanban progress`, `kanban_progress`,
and the dashboard progress API include the latest bounded
`worker_codex_events` list, so a controller can inspect recent Codex JSONL
activity without reading or interrupting the full Codex session.

Each worker instance records the worker lane, kind, task id, run id, worker pid, claim lock, workspace, and model in events and metadata. Codex output is written to the normal worker log (`hermes kanban log <task_id>`).

Operators can inspect the lane roster without interrupting workers:

```bash
hermes kanban worker-lanes --json
```

The dashboard also reads `GET /api/plugins/kanban/worker-lanes` and shows each registered external lane's kind, model, success policy, active/max concurrency, per-status counts, and active task/run/pid instances. This is a bounded status view; it does not read the full Codex session and does not claim, heartbeat, reclaim, or signal running workers.

The wrapper also heartbeats the task and parses these progress formats into `task_events` as `worker_progress`. Codex-specific heartbeat/progress events include `worker_lane`, `worker_kind`, and `run_id` so dashboards and controllers can group status by worker attempt without reading the worker log:

```text
o (1) 分析入口
x (2) 修改 dispatcher
```

```text
- [ ] 分析入口
- [x] 修改 dispatcher
```

On success, the default `block_for_review` policy blocks the task with structured evidence instead of marking it `done`:

```text
review-required: Codex completed; Hermes review required
```

The metadata includes bounded output tail, parsed receipt sections, an optional
structured verdict, git status, changed files, diff summary, verification
commands, and review reason. This is distinct from the `review` column's
profile-review dispatch path in current Kanban; Codex lane success hands
evidence to the Hermes controller without replaying the full Codex session. The
usual next step is to plan independent review/test worker tasks from that
evidence.

## Independent review/test follow-ups

Hermes should not act as the primary code reviewer for large coding tasks. The controller reads bounded evidence from the implementation worker and creates follow-up worker tasks:

```bash
hermes kanban plan-review <implementation_task_id> --json
```

By default this creates two idempotent tasks for the implementation run:

- `Review implementation evidence for <task_id>` assigned to `codex-review`
- `Verify implementation evidence for <task_id>` assigned to `codex-test`

For larger changes, Hermes can also create extra review shard tasks assigned to
the review lane. Shards are deterministic, evidence-bounded review tasks scoped
to subsets of changed files. They let Codex or another review worker inspect a
large diff in smaller pieces while Hermes still gates approval on structured
Kanban evidence instead of reading the full implementation session.

```yaml
kanban:
  deep_review:
    enabled: true
    changed_files_threshold: 8
    diff_summary_lines_threshold: 80
    max_files_per_shard: 8
    max_shards: 8
```

When `changed_files` or `diff_summary` evidence crosses those thresholds,
`plan-review` keeps the whole-diff review and test tasks, then adds
`Review shard <n> for <task_id>` tasks. Each shard body lists its file scope,
the same bounded implementation evidence, and requires a review verdict. This
is intentionally a review orchestration aid, not semantic approval by Hermes.

The CLI accepts `--review-assignee`, `--test-assignee`, `--review-only`, and `--test-only`. The Python tool surface exposes the same operation as `kanban_plan_review`, and the dashboard API exposes:

```text
POST /api/plugins/kanban/tasks/<task_id>/plan-review
```

The follow-up task bodies contain bounded implementation evidence: worker lane identity, source run id, changed files, diff summary, verification commands, verification summary, and a bounded output tail. They instruct the review/test worker to inspect the workspace or diff as needed and return a structured `Verdict:` line, without marking the source implementation task done. Review workers must end with `Verdict: approve`, `Verdict: request_changes`, or `Verdict: blocked`; test workers must end with `Verdict: pass`, `Verdict: fail`, or `Verdict: blocked`. The Codex adapter parses this receipt into `worker_receipt.verdict`, `worker_lane.receipt`, and `verification.verdict`; text scanning of `output_tail` remains only as a compatibility fallback for older workers.

For scheduling, the follow-up tasks are independent `ready` tasks so a dispatcher can claim them immediately. They are also linked back as dependencies of the blocked implementation task, and the source task receives a `worker_review_followups_planned` event. Progress queries include those follow-ups even though the dependency edge points from follow-up to source:

```bash
hermes kanban progress <implementation_task_id> --children --json
```

This gives the main agent and dashboard a compact view of whether review and test workers are still ready, running, blocked, or done without interrupting any worker process.

Controllers that want to start those workers immediately can ask the planner to
run one dispatcher pass scoped only to the planned follow-up task ids:

```bash
hermes kanban plan-review <implementation_task_id> --dispatch --json
```

The equivalent tool flag is `kanban_plan_review(dispatch=true)`, and the
dashboard/API accepts `{"dispatch": true}` on
`POST /api/plugins/kanban/tasks/<task_id>/plan-review`. This scoped dispatch
does not pick unrelated ready cards from the board.

Approval is gated once follow-ups are planned. `hermes kanban review <task_id> approve` refuses to mark the implementation task done until every planned follow-up for the current implementation run has successful worker evidence. For the built-in Codex adapter, a follow-up must exit 0, block with `review.required: true`, and satisfy the purpose-specific structured verdict: whole-diff review and review-shard follow-ups must be `approve`, and test follow-ups must be `pass`. Pending, running, missing, timed out, binary-missing, nonzero-exit, missing/mismatched `request_changes`, `fail`, or `blocked` follow-up verdicts block approval with an explicit gate error. Hermes still reviews the bounded receipt, not the full Codex session. `request-changes` remains available at any time and unblocks the implementation task for another worker run.

Hermes can also run deterministic local acceptance checks before approval. Configure named checks under `kanban.acceptance_checks`; each check is a fixed argv list, not a shell string produced by a model:

```yaml
kanban:
  acceptance_checks:
    unit-tests:
      argv: ["python3", "-m", "pytest", "-q"]
      timeout_seconds: 300
      description: "project unit tests"
```

Run them with `hermes kanban verify <implementation_task_id> unit-tests --json`, the `kanban_verify` tool, or `POST /api/plugins/kanban/tasks/<task_id>/verify`. The check runs in the task workspace, strips proxy environment variables, records bounded stdout/stderr, exit code, duration, and an `acceptance_check_completed` event, then `acceptance` exposes an `acceptance_check_gate`. If checks are configured, approval requires them to pass as well as any review/test follow-up gate.

For task-specific acceptance criteria, an orchestrator can attach a validated
task-scoped acceptance request instead of editing global config. The first
supported request type is `file_content`, which compares a
workspace-relative file against literal expected text. It is intentionally not
an arbitrary command runner:

```yaml
acceptance_check_request:
  name: expected-readme
  type: file_content
  path: README.md
  contains: "installation complete"
  description: "README mentions the installed state"
```

Attach it with:

```bash
hermes kanban acceptance-check-request <implementation_task_id> request.yaml --json
```

The Python tool surface is `kanban_acceptance_check_request`. Orchestrators can
also attach one request or a list of requests directly when creating a child
task:

```python
task = kanban_create(
    title="Implement README install note",
    assignee="codex-deep",
    body="Update README.md so it clearly documents the installed state.",
    acceptance_check_request={
        "name": "expected-readme",
        "type": "file_content",
        "path": "README.md",
        "contains": "installation complete",
    },
)["task_id"]
```

The dashboard API route is:

```text
POST /api/plugins/kanban/tasks/<task_id>/acceptance-check-requests
```

The validator allows only known request types, workspace-relative paths, and
literal `equals` or `contains` text. It rejects `argv`, `command`, `cmd`,
`shell`, and other executable fields. Requests created before the implementation
worker starts apply to that task's later source run; requests created after a
run exists are scoped to that run. `kanban_verify`, `advance-acceptance`, and
`advance-goal` run both configured checks and validated task-scoped requests.

For task-specific tests, configure trusted templates once and let the
orchestrator choose a template plus allowlisted arguments:

```yaml
kanban:
  acceptance_templates:
    pytest-target:
      argv_template: ["python3", "-m", "pytest", "{target}", "-q"]
      allowed_args: ["target"]
      arg_types:
        target: relative_path
      timeout_seconds: 300
      description: "Run pytest for one workspace-relative target"
```

Then attach a request:

```yaml
acceptance_check_request:
  name: focused-unit-test
  type: command_template
  template: pytest-target
  args:
    target: tests/test_widget.py
  description: "Run the focused unit test for this change"
```

The executable and argv shape still come from trusted `config.yaml`; the request
cannot change `argv_template[0]`, cannot add unlisted args, and
`relative_path` args cannot be absolute, escape the workspace, or begin with
`-`. This gives Hermes a task-specific way to run real tests while keeping
model output out of the command-construction trust boundary.

Before deciding, controllers can read a single acceptance snapshot:

```bash
hermes kanban acceptance <implementation_task_id> --json
```

The Python tool equivalent is `kanban_acceptance`, and the dashboard/API route is:

```text
GET /api/plugins/kanban/tasks/<task_id>/acceptance
```

This snapshot combines implementation evidence, planned review/test follow-up evidence, a compact `followup_summary`, the follow-up gate, Hermes-run acceptance check results, `approval_allowed`, `request_changes_allowed`, and a deterministic `recommended_action`. For review shards and failed follow-up workers, the summary and gate items include shard counts, file counts, scoped file samples, verdicts, runtime failure reasons, and worker metadata so the main agent and dashboard can inspect progress without opening full worker sessions. It is still bounded evidence; it does not replay full external-worker sessions.
The dashboard task drawer reads the same bounded acceptance snapshot for
review-required worker tasks. Operators can see the recommended next action,
review/test gate, review shard file scopes, acceptance-check gate, and can
click **Advance acceptance** to move the workflow to the next safe boundary
without interrupting any running worker.

Controllers can also advance the whole acceptance workflow one safe step at a time:

```bash
hermes kanban advance-acceptance <implementation_task_id> --json
hermes kanban advance-acceptance <implementation_task_id> --loop --json
```

The command reads the same acceptance snapshot and then performs only the next
deterministic control-plane action:

- plan missing review/test follow-up tasks;
- optionally run one dispatcher pass scoped only to pending follow-ups;
- run a maintenance-only dispatcher pass for already-running follow-ups so
  crashed or timed-out workers are detected without spawning duplicates;
- return if implementation, review, or test workers are still running;
- run configured Hermes acceptance checks once worker evidence is ready;
- request changes with bounded failure evidence when a review/test or
  acceptance gate deterministically fails;
- approve the implementation task only when every gate passes.

It never waits for, signals, or replays a running Codex worker. Use
`--loop` when a controller should keep making bounded deterministic progress
until the task is done, idle, waiting on a running worker, blocked, or the
`--max-iterations` cap is reached. Without `--loop`, one invocation performs
one safe step. Use `--no-dispatch`, `--dispatch-max`, `--dry-run`,
`--no-verify`, or `--no-approve` to stop at a specific boundary. Failed gates
default to `request-changes`, which writes a reviewer comment containing only
bounded follow-up verdicts, worker metadata, verification summaries, runtime
failure reasons, and deterministic check output tails, then unblocks the
implementation for another worker run.
On the next claim, the worker context includes a dedicated
`Requested changes to address before finishing` section with the latest
reviewer, source run id, timestamp, and bounded comment, so an implementation
Codex lane can fix the failure without the main agent replaying the full
session or manually restating the failure. Automatic request-changes feedback
is bounded by the task's `max_retries` value, or by `kanban.failure_limit` when
the task has no override. Once that limit is reached, the controller leaves the
implementation task blocked and records `worker_review_auto_retry_exhausted`
instead of dispatching another coding run; a human or higher-level planner can
still decide what to do next. Use `--no-request-changes` or API/tool
`request_changes_on_failure=false` when a controller wants to inspect the failed
gate without mutating task state. The Python tool equivalent is
`kanban_advance_acceptance`, and the dashboard/API route is:

```text
POST /api/plugins/kanban/tasks/<task_id>/advance-acceptance
```

For decomposed goal/root tasks, use the root-level controller:

```bash
hermes kanban advance-goal <goal_or_root_task_id> --json
hermes kanban advance-goal <goal_or_root_task_id> --loop --json
# Inside the originating session, the root id may be omitted:
hermes kanban advance-goal --loop --json
```

`advance-goal` reads the root's child progress without interrupting workers,
dispatches only ready child tasks for that root, advances any review-required
child through the same review/test/acceptance workflow, and completes the root
only after all related child tasks are `done` or `archived`. If a child
follow-up or acceptance gate fails, the default behavior is the same bounded
`request-changes` feedback on that child, leaving the root incomplete until the
child is rerun and accepted. That feedback is scoped to the failed
implementation run: the next implementation claim gets the latest requested
changes in its worker context, and review/test follow-ups from the old run do
not satisfy the approval gate for the new run. With `--loop`, the root
controller repeats these safe passes. For example, if a review follow-up
returns `Verdict: request_changes`, the controller can write bounded feedback,
redispatch the child implementation lane, and then stop at the running worker
boundary without interrupting it. The tool/API equivalents are
`kanban_advance_goal` and:

```text
POST /api/plugins/kanban/tasks/<task_id>/advance-goal
```

When an orchestrator is operating inside the same chat session that created the
root with `/goal create`, both `hermes kanban advance-goal --loop` and
`kanban_advance_goal(loop=true)` may omit the root task id. They resolve the
current `HERMES_SESSION_ID` session's latest explicit `/goal create` Kanban
root, then advance that root through the same scoped controller path. This keeps
control-plane actions session-local without interrupting running Codex workers.

For goal/root tasks, the dashboard task drawer reads child progress with
`children=true`, displays each child's compact acceptance state and next
action, and exposes **Advance goal** for the same scoped controller pass.

## Skill lane intent

Hermes skills can choose an existing lane directly:

```text
assignee=codex-deep
```

The decomposer and skills may only choose lane names already registered in the roster. Unknown assignees are rewritten to the configured `default_assignee`; model output cannot invent an executable lane by naming it.

Skills may also propose a lane request:

```yaml
worker_lane_request:
  name: codex-long-context
  type: codex_cli
  model: gpt-5.5
  sandbox: workspace-write
  approval: never
  max_concurrency: 1
  success_policy: block_for_review
  reason: "large refactor requiring stronger reasoning"
```

Model output is not trusted execution config. Requests must pass a deterministic validator: type allowlist, model allowlist, sandbox allowlist, approval allowlist, max concurrency cap, fixed command shape, and no arbitrary shell command fields.
For the Codex adapter, the validator also accepts only a boolean
`json_events` flag; it never accepts an arbitrary event command or shell
pipeline.
The built-in lane-request validator currently enables only the trusted
`codex_cli` adapter. Other external workers, such as Claude Code, OpenCode,
containers, or local services, should be added by a trusted plugin calling
`ctx.register_worker_lane(...)` until those adapters have their own validators.

Operators can validate a request without enabling it:

```bash
hermes kanban worker-lane-request request.yaml --json
```

Configured orchestrator profiles with the `kanban` toolset can use the same
controlled path without shelling out:

```text
kanban_worker_lane_request({
  "worker_lane_request": {
    "name": "codex-long-context",
    "type": "codex_cli",
    "model": "gpt-5.5",
    "sandbox": "workspace-write",
    "approval": "never",
    "max_concurrency": 1,
    "success_policy": "block_for_review",
    "reason": "large refactor requiring stronger reasoning"
  }
})
```

The tool defaults to validate-only. A trusted orchestrator can pass
`enable=true` to register the lane in the current Hermes process, or
`persist=true` to write sanitized adapter fields to `config.yaml` and register
the lane. Dispatcher-spawned workers cannot see or call this tool.

Dashboard/plugin clients can use the same validator through:

```text
POST /api/plugins/kanban/worker-lane-requests
```

The dashboard worker-lane panel exposes the same path as a controlled request
form. It lets an operator validate, enable, persist, or replace a Codex lane
request using the allowlisted fields; it does not accept arbitrary shell
commands.

By default the endpoint only validates. Pass `enable=true` to register the
lane for the current process, or `persist=true` to write the sanitized adapter
fields under `kanban.worker_lanes`.

After approval, enable it for the current Hermes process, or persist the sanitized config to `config.yaml`:

```bash
hermes kanban worker-lane-request request.yaml --enable
hermes kanban worker-lane-request request.yaml --persist
```

`--persist` writes only the sanitized adapter fields under `kanban.worker_lanes`; it does not store arbitrary command strings or the model's free-form reason.
For a standalone shell invocation, prefer `--persist` when a later dispatcher process must see the lane; `--enable` is mainly useful for in-process slash/gateway calls.

## Progress queries

Progress queries should read Kanban state, events, logs, and run metadata:

- `hermes kanban progress <task_id> --json`
- `hermes kanban acceptance <task_id> --json`
- `hermes kanban advance-acceptance <task_id> --json`
- `hermes kanban advance-goal <goal_or_root_task_id> --json`
- `hermes kanban progress <goal_or_root_task_id> --children --json`
- `hermes kanban reviews --json`
- `GET /api/plugins/kanban/tasks/<task_id>/progress`
- `GET /api/plugins/kanban/tasks/<task_id>/acceptance`
- `POST /api/plugins/kanban/tasks/<task_id>/advance-acceptance`
- `POST /api/plugins/kanban/tasks/<task_id>/advance-goal`
- `GET /api/plugins/kanban/tasks/<task_id>/progress?children=true`
- `GET /api/plugins/kanban/reviews`
- `hermes kanban show <task_id>`
- `hermes kanban tail <task_id>`
- `hermes kanban log <task_id>`
- `hermes kanban runs <task_id> --json`

These reads do not interrupt a running external worker.
Progress snapshots also attach active diagnostics for the root task and, when
children are included, for each child. This lets `kanban_progress`,
`hermes kanban progress --children`, `/goal status`, and the dashboard progress
drawer explain states such as `acceptance_check_gate_failed` or
`auto_request_changes_exhausted` from bounded Kanban evidence without opening or
interrupting the Codex session.
For Codex JSONL lanes, the same snapshots include recent bounded
`worker_codex_events` summaries for the task and each child, including known
thread, item, command, file-change, and usage fields.

`hermes kanban reviews` lists implementation handoffs whose latest run metadata
says `review.required: true`, optionally filtered with `--assignee`, `--tenant`,
or `--lane`. Review/test follow-up rows also store review-required evidence, but
they are consumed by the source task's follow-up gate and are hidden from this
queue by default; use `--include-followups` only when debugging those rows.
This review queue reads the bounded evidence already written to
`task_runs.metadata`, the latest progress event, and an optional worker-log tail
without replaying the complete Codex session.

Reviewers can close the handoff through the same bounded-evidence path:

```bash
hermes kanban plan-review <task_id> --json
hermes kanban plan-review <task_id> --dispatch --json
hermes kanban review <task_id> approve --summary "bounded evidence accepted"
hermes kanban review <task_id> request-changes --comment "add a regression test"
```

The dashboard/API equivalent is `POST /api/plugins/kanban/tasks/<task_id>/review`
with `decision=approve` or `decision=request_changes`.

`kanban plan-review` creates the independent review/test worker tasks.
`approve` records the final controller decision and marks the implementation
task done only after the planned follow-up gate is satisfied. `request-changes`
records the reviewer comment, emits a review event, and unblocks the
implementation task so the dispatcher can hand the follow-up back to the
assigned lane.

Configured orchestrator/main-agent profiles can use the equivalent tools:
`kanban_reviews` for the queue, `kanban_progress` for one task's bounded
snapshot, `kanban_acceptance` for implementation plus review/test evidence,
`kanban_acceptance_check_request` to attach validated task-scoped acceptance
checks, `kanban_verify` to run configured deterministic Hermes-side checks and
task-scoped requests,
`kanban_plan_review` to create and optionally dispatch independent review/test
follow-ups, `kanban_advance_acceptance` to move the control-plane workflow to
the next safe boundary, `kanban_advance_goal` to advance decomposed goal/root
tasks across their children, and `kanban_review` to approve or request changes.
These tools are orchestrator-only; dispatcher-spawned Codex workers do not see
them.

Pass `include_children=true` to `kanban_progress` when the task is a goal/root
task and the controller needs a compact status roll-up without interrupting
running workers. If `task_id` is omitted, `kanban_progress` and
`hermes kanban progress --children` look up the current `HERMES_SESSION_ID`
session's latest explicit `/goal create` Kanban root and return that root
snapshot with `resolved_from_session_goal=true`. This lets the main agent
answer "how is my goal going?" from Kanban state even when it did not keep the
root id in conversation context. The matching control-plane action is
`kanban_advance_goal(loop=true)` or `hermes kanban advance-goal --loop`, which
can also omit `task_id` for that current-session goal root. The snapshot includes
`child_summary` counts and a bounded
`children` list with each related worker task's relationship, status, lane,
latest run state, latest progress checklist, latest heartbeat event,
review-required flag, verification evidence, and compact acceptance state. The
acceptance state includes each child's deterministic `recommended_action`,
bounded review/test and acceptance gate summaries, and whether automatic
request-changes retries are exhausted. Child snapshots also include active
`diagnostics` and compact `warnings` when a controller or operator needs to know
why a review-required child is stuck. For ordinary graphs this summarizes
direct child tasks. For decomposed goals, Hermes also summarizes the worker
tasks recorded in the root task's `decomposed.child_ids` event, because the
current decomposer links those worker tasks as dependencies that wake the root
when complete.

## Goal bridge

The intended `/goal` bridge is:

```text
/goal create "complex objective"
-> create_kanban_task_from_goal(...)
-> orchestrator creates child tasks
-> child tasks use assignee=<lane_name>
-> external lanes execute
-> Hermes reviews Kanban evidence and responds to the user
```

The current `/goal` session-level semantics remain intact. The opt-in bridge is available through Kanban today:

```bash
hermes kanban goal "complex objective" --assignee orchestrator --session <session-id>
hermes kanban goal "complex objective" --assignee orchestrator --workspace dir:/repo --decompose
hermes kanban goal "complex objective" --assignee orchestrator --workspace dir:/repo --decompose --advance
hermes kanban goal "complex objective" --assignee orchestrator --workspace dir:/repo --decompose --advance --loop
```

Gateway `/goal create ...` uses the same bridge and parser:

```text
/goal create complex objective --assignee orchestrator --workspace dir:/repo --decompose --advance --loop
```

`--decompose` runs the existing Kanban decomposer immediately. Its child tasks
inherit the root task's workspace, branch, tenant, priority, runtime cap, retry
cap, and session id, so a goal created with `--workspace dir:/repo` can fan out
Codex child tasks that work in the intended repository without manual DB
patching. The child tasks can use worker lane assignees from the registry, such
as `codex-deep`, and the dispatcher later starts those external workers. Add
`--advance` when the create call should immediately run one scoped root
controller pass after decomposition. That pass dispatches only ready children
belonging to the new root and can plan/advance review or test follow-ups if the
root already has review-required children. It still does not wait for or
interrupt running workers; progress queries should keep reading Kanban state.
Add `--loop` when the create call should repeat bounded controller passes until
the root is done, blocked, idle, waiting on running worker lanes, or the
iteration cap is reached.

After decomposition, a controller can repeatedly call:

```bash
hermes kanban progress <goal_or_root_task_id> --children --json
hermes kanban advance-goal <goal_or_root_task_id> --json
hermes kanban advance-goal <goal_or_root_task_id> --loop --json
```

The first command reports child status; the second performs one safe
control-plane action for the root and its children; the loop form keeps making
safe deterministic progress until the next async boundary.

Gateway deployments can run the same bounded controller automatically after
each dispatcher tick:

```yaml
kanban:
  dispatch_in_gateway: true
  advance_controller_in_gateway: true
  advance_controller_max_items: 8
  advance_controller_max_iterations: 8
  advance_controller_dispatch_max: 8
  advance_controller_review_assignee: codex-review
  advance_controller_test_assignee: codex-test
  advance_controller_request_changes_on_failure: true
```

The controller tick scans decomposed goal roots and standalone
review-required implementation tasks, advances each to the next idle boundary,
and then returns. It does not poll or interrupt running Codex workers. Disable
it with `kanban.advance_controller_in_gateway: false` or the
`HERMES_KANBAN_ADVANCE_CONTROLLER_IN_GATEWAY=0` environment override when an
operator wants manual-only advancement.

For an explicit one-shot run, use:

```bash
hermes kanban advance-controller --json
hermes kanban controller-tick --dispatch-max 2 --json
```

The orchestrator tool/API equivalents are `kanban_advance_controller` and:

```text
POST /api/plugins/kanban/advance-controller
```

## Failure modes the dispatcher handles

So lane authors don't have to reimplement these:

- **Stale claim TTL** — a worker that claims and then never heartbeats / completes / blocks gets reclaimed after `DEFAULT_CLAIM_TTL_SECONDS` (15 min default) — but only if the worker process has actually died. A live worker (slow model spending 20+ min in one tool-free LLM call) gets the claim *extended* instead of killed; only a dead PID is reclaimed.
- **Crashed worker** — a worker whose host-local PID has vanished is detected by `detect_crashed_workers` and reaped; the task increments `consecutive_failures` and may auto-block when the breaker trips.
- **Run-level retry** — when a task is retried (post-block, post-crash, post-reclaim), the worker can use the `expected_run_id` parameter on terminating tools to fail fast if its own run was already superseded.
- **Per-task max runtime** — `task.max_runtime_seconds` hard-caps wall-clock time per run, regardless of PID liveness. Catches genuinely-deadlocked workers that the live-PID extension would otherwise keep running.
- **Stranded-task detection** — a ready task whose assignee never produces a claim within `kanban.stranded_threshold_seconds` (default 30 min) shows up in `hermes kanban diagnostics` as a `stranded_in_ready` warning. Severity escalates to error at 2x the threshold and critical at 6x. Catches typo'd assignees, deleted profiles, and down external worker pools in one signal — identity-agnostic, no per-board allowlist to curate.
- **Acceptance-gate diagnostics** — a review-required implementation whose current run has failed deterministic Hermes acceptance checks shows `acceptance_check_gate_failed` in `hermes kanban diagnostics`, dashboard diagnostics, and task drawer warnings. The diagnostic lists the failed check names, exit/tail evidence, and links operators to `hermes kanban acceptance <task_id> --json` or a controller advance/request-changes path. It clears when the check passes for that run, the task is approved, or request-changes/unblock moves the implementation into a new run.
- **Retry-exhausted diagnostics** — when automatic request-changes reaches the task/config retry limit, diagnostics show `auto_request_changes_exhausted`. This means the controller has deliberately stopped looping; an operator or main agent should inspect bounded acceptance/follow-up evidence, add targeted guidance, reassign, or change the retry policy intentionally.

## Current limits

- Codex JSONL event ingestion is optional per lane (`json_events: true`).
  Without it, the wrapper still falls back to parsing stdout/stderr. Even in
  JSONL mode Hermes stores bounded event summaries and maps only known event
  shapes into progress, receipt, command, and usage evidence.
- No approval bridge; configure Codex lanes with controlled approval policy.
- Follow-up gating understands structured verdicts (`approve` for whole-diff and shard review, `pass` for test), and Hermes can run configured deterministic acceptance commands plus validated task-scoped `file_content` checks. Failed gates can be fed back as bounded `request-changes` comments. Large diffs can be split into review shards, but Hermes still relies on external reviewer verdicts and deterministic checks rather than performing its own semantic code review.
- External lane command shapes are adapter-defined, not model-defined.
- Review reads Codex artifacts and bounded metadata, not the full Codex session.

## Related

- [Kanban overview](./kanban) — the user-facing intro.
- [Kanban tutorial](./kanban-tutorial) — walkthrough with the dashboard open.
- [`kanban-worker`](https://github.com/NousResearch/hermes-agent/blob/main/skills/devops/kanban-worker/SKILL.md) — the skill the worker process loads.
- [`kanban-orchestrator`](https://github.com/NousResearch/hermes-agent/blob/main/skills/devops/kanban-orchestrator/SKILL.md) — the orchestrator side.
