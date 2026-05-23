---
name: kanban-orchestrator
description: Decomposition playbook + anti-temptation rules for an orchestrator profile routing work through Kanban. The "don't do the work yourself" rule and the basic lifecycle are auto-injected into every kanban worker's system prompt; this skill is the deeper playbook when you're specifically playing the orchestrator role.
version: 3.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, multi-agent, orchestration, routing]
    related_skills: [kanban-worker]
---

# Kanban Orchestrator — Decomposition Playbook

> The **core worker lifecycle** (including the `kanban_create` fan-out pattern and the "decompose, don't execute" rule) is auto-injected into every kanban process via the `KANBAN_GUIDANCE` system-prompt block. This skill is the deeper playbook when you're an orchestrator profile whose whole job is routing.

## Assignees Are User-Configured — Not a Fixed Roster

Hermes setups vary widely. Some users run a single profile that does everything; some run a small fleet (`docker-worker`, `cron-worker`); some run external worker lanes such as `codex-deep`, `codex-review`, or `codex-test`; some run a curated specialist team they've named themselves. There is **no default specialist roster** — the orchestrator skill does not know what assignees exist on this machine.

Before fanning out, ground the decomposition in the Hermes profiles and registered worker lanes that actually exist. The dispatcher only spawns known profiles or trusted worker lanes. Unknown terminal/control-plane names remain unspawned and appear as `skipped_nonspawnable`, so a card assigned to a made-up `researcher` on a setup that only has `docker-worker` and `codex-deep` just sits in `ready`.

**Step 0: discover available assignees before planning.**

Use one of these:

- `hermes profile list` — prints Hermes profiles configured on this machine.
- `hermes kanban worker-lanes --json` — prints trusted external worker lanes, capacity, and active instances.
- `hermes kanban assignees --json` — prints the combined board assignee view, including worker lanes when available.
- `kanban_worker_lane_request(...)` — propose a new lane only as a structured request through the deterministic validator; never invent a lane by simply assigning a task to a new name.
- **Just ask the user.** "What profiles or worker lanes do you have set up?" is a fine first turn when the goal needs more than one specialist.

Cache the result in your working memory for the rest of the conversation. Re-asking every turn wastes a tool call.

## When to use the board (vs. just doing the work)

Create Kanban tasks when any of these are true:

1. **Multiple specialists are needed.** Research + analysis + writing is three profiles.
2. **The work should survive a crash or restart.** Long-running, recurring, or important.
3. **The user might want to interject.** Human-in-the-loop at any step.
4. **Multiple subtasks can run in parallel.** Fan-out for speed.
5. **Review / iteration is expected.** A reviewer profile loops on drafter output.
6. **The audit trail matters.** Board rows persist in SQLite forever.

If *none* of those apply — it's a small one-shot reasoning task — use `delegate_task` instead or answer the user directly.

## The anti-temptation rules

Your job description says "route, don't execute." The rules that enforce that:

- **Do not execute the work yourself.** Your restricted toolset usually doesn't even include terminal/file/code/web for implementation. If you find yourself "just fixing this quickly" — stop and create a task for the right specialist.
- **For any concrete task, create a Kanban task and assign it.** Every single time.
- **Split multi-lane requests before creating cards.** A user prompt can contain several independent workstreams. Extract those lanes first, then create one card per lane instead of bundling unrelated work into a single implementer card.
- **Run independent lanes in parallel.** If two cards do not need each other's output, leave them unlinked so the dispatcher can fan them out. Link only true data dependencies.
- **Never create dependent work as independent ready cards.** If a card must wait for another card, pass `parents=[...]` in the original `kanban_create` call. Do not create it first and link it later, and do not rely on prose like "wait for T1" inside the body.
- **If no specialist fits the available assignees, ask the user which profile/lane to use or submit a worker lane request.** Do not invent profile or lane names; the dispatcher will not treat model output as trusted execution config.
- **Decompose, route, and summarize — that's the whole job.**

## Decomposition playbook

### Step 1 — Understand the goal

Ask clarifying questions if the goal is ambiguous. Cheap to ask; expensive to spawn the wrong fleet.

### Step 2 — Sketch the task graph

Before creating anything, draft the graph out loud (in your response to the user). Treat every concrete workstream as a candidate card:

1. Extract the lanes from the request.
2. Map each lane to one of the profiles or worker lanes you discovered in Step 0. Coding implementation usually belongs on an execution lane such as `codex-deep`; independent review and verification can use `codex-review` and `codex-test` when configured.
3. Decide whether each lane is independent or gated by another lane.
4. Create independent lanes as parallel cards with no parent links.
5. Create synthesis/review/integration cards with parent links to the lanes they depend on. A child created with unfinished parents starts in `todo`; the dispatcher promotes it to `ready` only after every parent is done.

Examples of prompts that should fan out (using placeholder assignee names — substitute whatever exists on the user's setup):

- "Build an app" → one card to a design-oriented profile for product/UI direction, one or two cards to engineering profiles for implementation, plus a later integration/review card if the user has a reviewer profile.
- "Fix blockers and check model variants" → one implementation card for the blocker fixes plus one discovery/research card for config/source verification. A final reviewer card can depend on both.
- "Research docs and implement" → a docs-research card can run in parallel with a codebase-discovery card; implementation waits only if it truly needs those findings.
- "Analyze this screenshot and find the related code" → one card to a vision-capable profile for the visual analysis while another searches the codebase.

Words like "also," "finally," or "and" do not automatically imply a dependency. They often mean "make sure this is covered before reporting back." Only link tasks when one card cannot start until another card's output exists.

Show the graph to the user before creating cards. Let them correct it — including which actual profile or worker lane should own each lane.

### Step 3 — Create tasks and link

Use the assignee names from Step 0. The example below uses placeholders `<assignee-A>`, `<assignee-B>`, `<assignee-C>` — replace them with the actual Hermes profile or worker lane names on this setup.

```python
t1 = kanban_create(
    title="research: Postgres cost vs current",
    assignee="<assignee-A>",  # whichever profile/lane handles research on this setup
    body="Compare estimated infrastructure costs, migration costs, and ongoing ops costs over a 3-year window. Sources: AWS/GCP pricing, team time estimates, current Postgres bills from peers.",
    tenant=os.environ.get("HERMES_TENANT"),
)["task_id"]

t2 = kanban_create(
    title="research: Postgres performance vs current",
    assignee="<assignee-A>",  # same assignee, run in parallel
    body="Compare query latency, throughput, and scaling characteristics at our expected data volume (~500GB, 10k QPS peak). Sources: benchmark papers, public case studies, pgbench results if easy.",
)["task_id"]

t3 = kanban_create(
    title="synthesize migration recommendation",
    assignee="<assignee-B>",  # whichever profile/lane does synthesis/analysis
    body="Read the findings from T1 (cost) and T2 (performance). Produce a 1-page recommendation with explicit trade-offs and a go/no-go call.",
    parents=[t1, t2],
)["task_id"]

t4 = kanban_create(
    title="draft decision memo",
    assignee="<assignee-C>",  # whichever profile/lane drafts user-facing prose
    body="Turn the analyst's recommendation into a 2-page memo for the CTO. Match the tone of previous decision memos in the team's knowledge base.",
    parents=[t3],
)["task_id"]
```

`parents=[...]` gates promotion — children stay in `todo` until every parent reaches `done`, then auto-promote to `ready`. No manual coordination needed; the dispatcher and dependency engine handle it.

If the task graph has dependencies, create the parent cards first, capture their returned ids, and include those ids in the child card's `parents` list during the child `kanban_create` call. Avoid creating all cards in parallel and linking them afterward; that creates a window where the dispatcher can claim a child before its inputs exist.

When a child task has a concrete deterministic acceptance condition, attach it
in the same `kanban_create` call with `acceptance_check_request` or
`acceptance_check_requests`. These are declarative requests, not executable
commands: use `file_content` with a workspace-relative path plus exactly one of
`equals`/`contains`, or `command_template` selecting a trusted configured
template plus allowlisted args. Do not put `command`, `cmd`, `shell`, `argv`, or
`executable` in the request.

### Step 4 — Complete your own task

If you were spawned as a task yourself (e.g. a planner profile was assigned `T0: "investigate Postgres migration"`), mark it done with a summary of what you created:

```python
kanban_complete(
    summary="decomposed into T1-T4: 2 research lanes in parallel, 1 synthesis on their outputs, 1 prose draft on the recommendation",
    metadata={
        "task_graph": {
            "T1": {"assignee": "<assignee-A>", "parents": []},
            "T2": {"assignee": "<assignee-A>", "parents": []},
            "T3": {"assignee": "<assignee-B>", "parents": ["T1", "T2"]},
            "T4": {"assignee": "<assignee-C>", "parents": ["T3"]},
        },
    },
)
```

### Step 5 — Report back to the user

Tell them what you created in plain prose, naming the actual assignees you used:

> I've queued 4 tasks:
> - **T1** (`<assignee-A>`): cost comparison
> - **T2** (`<assignee-A>`): performance comparison, in parallel with T1
> - **T3** (`<assignee-B>`): synthesizes T1 + T2 into a recommendation
> - **T4** (`<assignee-C>`): turns T3 into a CTO memo
>
> The dispatcher will pick up T1 and T2 now. T3 starts when both finish. You'll get a gateway ping when T4 completes. Use the dashboard or `hermes kanban tail <id>` to follow along.

## External Worker Lanes

External worker lanes are execution lanes registered in the worker lane registry. They are not Hermes profiles and they are not model providers. Assigning a task to `codex-deep` means the Kanban dispatcher starts Codex CLI through the trusted adapter for that lane.

Use external worker lanes this way:

1. Create implementation tasks with `assignee` set to an existing execution lane, for example `codex-deep`.
2. Attach concrete acceptance checks during `kanban_create` when possible, so
   the controller can verify them after Codex returns bounded evidence.
3. Do not wait on or interrupt the running worker. Read progress with `kanban_progress(task_id=...)` or `hermes kanban progress <task_id> --json`. For the current session's explicit `/goal create` root, `kanban_progress(include_children=True)` can omit `task_id` and resolve the latest session goal root automatically.
4. When the Codex lane finishes, expect the task to be `blocked` with `review.required: true`, not `done`.
5. Advance the task with `kanban_advance_acceptance(..., loop=True)` or `hermes kanban advance-acceptance <task_id> --loop`. This plans independent review/test tasks, dispatches `codex-review` / `codex-test` when configured, runs deterministic Hermes acceptance checks, feeds bounded request-changes comments back to implementation lanes, and approves only when gates pass.
6. For decomposed roots, call `kanban_advance_goal(..., loop=True)` or `hermes kanban advance-goal <root_task_id> --loop` to dispatch children, advance review/test/acceptance, redispatch bounded reruns when gates request changes, and complete the root when all children are terminal. For the current session's explicit `/goal create` root, `kanban_advance_goal(loop=True)` can omit `task_id` and resolve the latest session goal root automatically. The loop stops at running-worker boundaries and does not interrupt Codex.
7. For unattended operation, prefer `kanban_advance_controller(...)` or let the gateway controller tick run from `kanban.advance_controller_in_gateway`. It scans decomposed roots and standalone review-required tasks, advances each to the next idle boundary, and never waits on or interrupts running workers.

When a needed external lane does not exist, submit a lane request instead of inventing an assignee:

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

The request defaults to validate-only. A trusted operator or orchestrator may pass `enable=True` for the current process or `persist=True` to write sanitized config. The validator rejects arbitrary command, shell, argv, and executable fields.

## Common patterns

**Fan-out + fan-in (research → synthesize):** N research-style cards with no parents, one synthesis card with all of them as parents.

**Parallel implementation + validation:** one implementer card makes the change while one explorer/researcher card verifies config, docs, or source mapping. A reviewer card can depend on both. Do not make the implementer own unrelated verification just because the user mentioned both in one sentence.

**Pipeline with gates:** `planner → implementer → reviewer`. Each stage's `parents=[previous_task]`. Reviewer blocks or completes; if reviewer blocks, the operator unblocks with feedback and respawns.

**Same-profile queue:** N tasks, all assigned to the same profile, no dependencies between them. Dispatcher serializes — that profile processes them in priority order, accumulating experience in its own memory.

**Human-in-the-loop:** Any task can `kanban_block()` to wait for input. Dispatcher respawns after `/unblock`. The comment thread carries the full context.

## Pitfalls

**Inventing profile or lane names that don't exist.** The dispatcher will not spawn unknown assignees — the card just sits in `ready` and appears in skipped/nonspawnable diagnostics. Always assign to a profile or worker lane from your Step 0 discovery; ask the user or submit a validated lane request if you're unsure.

**Bundling independent lanes into one card.** If the user asks for two independent outcomes, create two cards. Example: "fix blockers and check model variants" is not one fixer task; create a fixer/engineer card for the fixes and an explorer/researcher card for the variant check, then optionally gate review on both.

**Over-linking because of wording.** "Finally check X" may still be parallel with implementation if X is static config, docs, or source discovery. Link it after implementation only when the check depends on the implementation result.

**Forgetting dependency links.** If the task graph says `research -> implement -> review`, do not create all tasks as independent ready cards. Use parent links so implement/review cannot run before their inputs exist.

**Reassignment vs. new task.** If a reviewer blocks with "needs changes," create a NEW task linked from the reviewer's task — don't re-run the same task with a stern look. The new task is assigned to the original implementer profile.

**Argument order for links.** `kanban_link(parent_id=..., child_id=...)` — parent first. Mixing them up demotes the wrong task to `todo`.

**Don't pre-create the whole graph if the shape depends on intermediate findings.** If T3's structure depends on what T1 and T2 find, let T3 exist as a "synthesize findings" task whose own first step is to read parent handoffs and plan the rest. Orchestrators can spawn orchestrators.

**Tenant inheritance.** If `HERMES_TENANT` is set in your env, pass `tenant=os.environ.get("HERMES_TENANT")` on every `kanban_create` call so child tasks stay in the same namespace.

## Recovering stuck workers

When a worker profile keeps crashing, hallucinating, or getting blocked by its own mistakes (usually: wrong model, missing skill, broken credential), the kanban dashboard flags the task with a ⚠ badge and opens a **Recovery** section in the drawer. Three primary actions:

1. **Reclaim** (or `hermes kanban reclaim <task_id>`) — abort the running worker immediately and reset the task to `ready`. The existing claim TTL is ~15 min; this is the fast path out.
2. **Reassign** (or `hermes kanban reassign <task_id> <new-profile> --reclaim`) — switch the task to a different profile (one that exists on this setup) and let the dispatcher pick it up with a fresh worker.
3. **Change profile model** — the dashboard prints a copy-paste hint for `hermes -p <profile> model` since profile config lives on disk; edit it in a terminal, then Reclaim to retry with the new model.

Hallucination warnings appear on tasks where a worker's `kanban_complete(created_cards=[...])` claim included card ids that don't exist or weren't created by the worker's profile (the gate blocks the completion), or where the free-form summary references `t_<hex>` ids that don't resolve (advisory prose scan, non-blocking). Both produce audit events that persist even after recovery actions — the trail stays for debugging.
