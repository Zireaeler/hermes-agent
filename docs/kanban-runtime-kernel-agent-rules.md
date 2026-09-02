## Runtime Kernel Branch Constraints

When working on branch `feature-kanban-runtime-kernel`, treat
`docs/kanban-runtime-kernel-design.md` and
`docs/kanban-runtime-kernel-roadmap.md` and
`docs/kanban-runtime-kernel-phase1.md` and
`docs/kanban-runtime-kernel-phase2.md` and
`docs/kanban-runtime-kernel-phase2b.md` and
`docs/kanban-runtime-kernel-phase2c.md` and
`docs/kanban-runtime-kernel-phase2d.md` and
`docs/kanban-runtime-kernel-phase3.md` and
`docs/kanban-runtime-kernel-phase3b.md` and
`docs/kanban-runtime-kernel-phase3c.md` and
`docs/kanban-runtime-kernel-phase3d.md` and
`docs/kanban-runtime-kernel-phase4.md` and
`docs/kanban-runtime-kernel-phase4e.md` and
`docs/kanban-runtime-kernel-phase4f.md` and
`docs/kanban-runtime-kernel-phase4g0.md` and
`docs/kanban-runtime-kernel-phase4g7.md` and
`docs/kanban-runtime-kernel-phase4g8.md` and
`docs/kanban-runtime-kernel-phase4g9.md` and
`docs/kanban-runtime-kernel-phase4g9-iterative.md` and
`docs/kanban-runtime-kernel-phase4g10.md` and
`docs/kanban-runtime-kernel-phase4g10-clean-replay.md` and
`docs/kanban-runtime-kernel-phase4g11.md` and
`docs/kanban-runtime-kernel-phase4g12.md` and
`docs/kanban-runtime-kernel-phase4g13.md` and
`docs/kanban-runtime-kernel-phase4g14.md` and
`docs/kanban-runtime-kernel-phase4g15.md` and
`docs/kanban-runtime-kernel-phase4g16.md` and
`docs/kanban-runtime-validation-artifact-retention.md` as binding design
constraints, not background reading. If implementation details conflict with
those documents, update the relevant design document first or stop and ask for
direction.

`docs/kanban-runtime-kernel-delegation-policy.md` is also binding. Default to
one coherent primary execution node for work a capable continuous worker
session can complete. Do not decompose by role, phase, file, or local tool
step; durable graph expansion requires a documented structural reason such as
independent verification, capability boundary, workspace isolation, durable
parallelism, context/runtime limit, distinct deliverables, or execution-
discovered gap. Workers may not create durable runtime nodes, though a backend
may use ephemeral internal subagents within its inherited capability envelope.

Phase 4G12 evidence-driven graph mutation remains provider-proposed and
validator-controlled. A coordination checkpoint may include non-authoritative
responsibility candidates, but workers still cannot create durable nodes.
Dynamic expansion requires an exact checkpoint candidate reference, goal
linkage, isolated write scope, remaining child budget, an existing integration
owner, and evidence-backed decomposition. Ordinary coordination checkpoints
remain routing-only.

Phase 4G13 makes coordination event-driven rather than child-driven. Do not
force a first coordination checkpoint merely because a node is an isolated
child. A worker may include responsibility candidates in a terminal receipt;
Runtime must persist, validate, and consume those candidates exactly once.
Deterministic dependency/readiness transitions must not call the Decision
Provider. Pending terminal candidates may hold their integration owner until an
accepted patch either expands them or records an explicit no-expansion
resolution. More checkpoints, resumes, decisions, or nodes are not success
conditions; coordination overhead must be observable.

Phase 4G14 makes durable contribution capture independent from receipt
semantics. A terminal isolated child attempt must first produce an immutable,
hash-verified attempt patch artifact even when its receipt is missing or
invalid. Quarantined attempt artifacts are non-authoritative and must not
satisfy dependencies or goals; only a valid role-specific receipt may promote
the same captured patch into a formal contribution. Receipt repair must not
redo implementation. Replacement integration owners may inherit promoted
lineage only through validated DB edges, and worktree cleanup must refuse to
run until attempt artifacts and capture events are complete and verified.

Phase 4G15 adds evidence-driven live control and a mandatory orchestration
learning lifecycle for managed real validations. A live directive may steer an
active Codex turn only when its source checkpoint, target materialization,
thread, turn, contract revision, capability envelope, and non-expanding write
scope are validated. Failed or unsafe steering must remain a durable queued
directive; process termination is not a normal coordination mechanism. Every
managed real run must produce and register a hash-linked learning bundle before
archive cleanup, but findings and candidates may not automatically mutate
profiles, guidance, validators, capabilities, or graph policy. Promotion
requires reproducible baseline/treatment evidence, quality non-regression, and
explicit approval.

Phase 4G16 calibrates that live control through the normal production path. A
worker reports evidence and affected responsibilities but does not select the
coordination route. Runtime must persist one idempotent action audit per
checkpoint, locally route context-only effects without a Decision Provider,
and reserve provider calls for topology, ownership, scope, capability, human,
or new durable-responsibility changes. Validation harnesses may not directly
insert checkpoints, directives, candidate keys, or decomposition answers.
Natural calibration uses paired coherent/shared-contract/durable-boundary
tasks, preserves the Phase 4G15 learning/archive gate, and may not trade final
quality for orchestration activity.

Phase 4G16 deferred decomposition adds a third early assessment outcome between
immediate expansion and coherent continuation. Deferred candidates remain
non-authoritative until the same Primary session reaches its declared shared-
contract milestone. Runtime must then capture a hash-verified immutable seed
artifact and Runtime-owned Git revision before asking the Decision Provider to
change topology. Isolated children must start from that seed and contribute
only their post-seed delta; the Primary remains integration owner and waits for
those contributions. A harness may not fabricate the deferred candidate,
milestone, seed, or activation event, and no provider call is allowed merely to
persist or continue an unmet deferred milestone.

Runtime Kernel 的设计、roadmap、phase、真实验证和证据保留文档必须以中文作为规范性
叙述语言。函数名、类名、schema 字段、`event_type` 值、CLI 命令、API path、
model/provider 名称和引用的原始标识可以保留英文；解释性段落、章节说明、实施计划、
验收标准、测试说明、报告结论和 artifact catalog 说明必须使用中文。生成这些 Markdown
的代码也必须输出中文，避免后续运行重新生成英文文档。Worker、provider 或 evaluator
的原始输出属于证据，可以原样保留，不得为了满足语言规则而改写原始事实。

Do not continue the old Orchestra phase-manager architecture in this branch.
Do not reuse `kanban_orchestra.py` as the runtime kernel core, do not introduce
planner/coder/reviewer/tester phase semantics, and do not turn
`analysis -> implementation -> verification` into a default workflow. That path
is allowed only as a deterministic Phase 1 fixture driven by goal gaps and graph
patches.

The runtime kernel must remain goal-driven:
- DB rows are the authoritative state; decision sessions are non-authoritative
  inference context only.
- `goal_contracts` and `progress_ledger` define completion; execution graph
  completion does not.
- The local reducer owns readiness, job state, goal gaps, liveness, synthetic
  audits, and completion.
- Decision providers may propose graph patches only; they must not release
  nodes, directly complete jobs, directly write DB state, create Kanban tasks, or
  freely mark jobs blocked.
- Every new execution node must link to a goal item, a goal gap, or a human gate
  reason. Validator code must reject unlinked node creation.

Phase 1 implementation is intentionally narrow. Keep the first code path in
`hermes_cli/kanban_runtime_kernel.py` with focused tests in
`tests/hermes_cli/test_kanban_runtime_kernel.py`. Do not make real LLM calls,
dashboard/API work, a runtime daemon, full checkpoint compaction, old Orchestra
frontend migration, or real Codex smoke tests prerequisites for Phase 1.

Phase 2A is a control-plane and execution-substrate wiring stage. Add thin
`hermes kanban runtime ...` style entry points and tests that prove runtime
materialized tasks can flow through existing Kanban dispatcher/worker-lane
fixtures and back through `task_progress_snapshot()` ingest. Do not use this
stage to introduce a real LLM provider, a persistent runtime daemon, dashboard
UI migration, or concrete Codex/Claude Code smoke dependency.

Phase 2B is a decision-provider/session foundation stage. Keep DB facts
authoritative while adding cache-friendly provider input rendering,
DB-derived decision checkpoints, strict provider-output parsing, and
record/replay provider tests. Do not relax graph patch validation to
accommodate model output, and do not make live LLM/network calls a test
prerequisite.

Phase 2C is a goal-progression hardening stage. Strengthen local reducer,
progress ledger, goal gap detection, liveness, anti-stuck synthetic events, and
human-gate policy before adding a live LLM provider. Do not use model behavior
to compensate for missing local completion, readiness, gap, liveness, or
blocked-state rules.

Phase 2D is a decision-session compaction stage. Add active segment lifecycle,
DB-derived checkpoint creation, checkpoint validation, compaction policy/profile
boundaries, and provider input replacement before adding a live LLM provider.
Do not treat worker receipts, worker backend internal context compression, or
dashboard summaries as runtime decision-session compaction.

Phase 3 is a real decision-provider integration stage. Real providers may only
generate graph patch proposals from the Phase 2D provider request composition;
they must not own DB facts, create Kanban tasks, release nodes, directly
complete jobs, mark jobs blocked, or bypass parser/validator boundaries. Unit
tests for this stage must use fake/replay providers and must not require live
network calls or API keys.

For Phase 3 provider work, reuse Hermes provider substrate
(`resolve_runtime_provider`, auxiliary client/transport builders, timeout,
retry/backoff, usage/token helpers) but do not reuse the full `AIAgent`
conversation loop as the runtime decision provider. RuntimeDecisionProvider must
remain no-tools and single-shot: it must not pass `tools`, call web/search,
dispatch workers, use agent-level context compression, or silently fall back to
another model without runtime audit. Real provider calls must be explicit
(`--provider real` with model provider and model); default runtime advance must
not call a live model. `--codex-config` is allowed only as an explicit bridge
for manual smoke/integration runs: it may read `~/.codex/config.toml` and
`~/.codex/auth.json`, but must not modify Codex files or print/write API keys.

Phase 3B is a real-provider patch-quality hardening stage. Validator recovery
may feed rejected patch details and validator reasons back to the no-tools
provider during smoke/integration runs, but it must not relax validator rules,
auto-apply rejected patches, or become default behavior for normal runtime
advance unless a later design document explicitly allows that.

Phase 3C is a real-provider end-to-end runtime-loop hardening stage. Manual or
test evidence bridges may complete the latest materialized Kanban task for a
runtime node, but they must not directly mutate execution graph state, progress
ledger rows, goal item completion, graph revision, or decision records. Runtime
state changes still flow through `runtime advance` evidence ingest and the
local reducer. Default tests must remain offline; live `.codex` provider runs
are explicit integration smoke only.

Phase 3D is a long-running autonomous task runtime stage. Strategy updates must
be graph work (`strategy_update` nodes) rather than hidden provider memory or
direct DB mutation. User goal changes must enter DB facts through explicit
runtime APIs such as goal waiver, with reducer-owned completion. Resume
semantics must be DB-based: repeated `runtime advance` calls after process
boundaries must continue from persisted graph, ledger, events, decisions,
segments, and checkpoints.

Phase 4 is production hardening. Implement it as 4A real compaction provider,
4B observability/dashboard API, 4C production supervisor/recovery, and 4D
concurrency/safety hardening. Do not let compaction providers write DB or output
graph patches; do not remove deterministic compaction fallback; do not make
dashboard views mutate private tables directly; do not let supervisor hidden
memory become required for correctness.

Phase 4E is worker recovery and runtime consistency. Implement it before
opening a Phase 5 or prioritizing dashboard UI. Recovery must be a local,
deterministic reducer over execution_nodes, node_materializations, Kanban
tasks/runs/events, receipts, ledger, and policy; do not ask the decision
provider to decide whether a worker run is stale, missing, crashed, retryable,
or allowed to overwrite terminal facts. Preserve materialization attempt
history, never silently rewrite terminal node facts, and expose recovery /
consistency / legal waiting reason through runtime observability.

Phase 4F is runtime capability and security policy. Capability authorization
must be a local runtime policy boundary, not provider judgment. Decision
providers may request capabilities or propose a human gate, but they must not
grant themselves filesystem, network, secret, external-cost, destructive, git,
or database-migration privileges. Validator and materialization code must
evaluate requested capabilities before creating worker tasks, worker context
must include allowed/denied/requires-human capabilities, and policy blocks must
be observable as structured runtime state rather than hidden worker behavior.

Phase 4G0 is runtime memory hints. Runtime memory is non-authoritative context,
not a fact source and not a validator override. Stable hard rules belong in
runtime guidance; cross-job experience belongs in scoped memory topics; current
job continuity belongs in decision session checkpoints. Candidate memory must
not be injected by default, accepted memory must be scope-filtered, deprecated
memory must not be injected, and memory hints must never affect readiness,
completion, blocked state, capability policy, worker recovery, or graph patch
validation.

Phase 4G7 is the packaged runtime supervisor daemon stage. The daemon must
wrap the existing production supervisor tick and per-job DB lease; it must not
duplicate reducers or keep correctness-required job state in process memory,
PID files, health state, or operational state files. Each process start must
use a unique lease owner, real providers must remain explicit and bounded by a
timeout smaller than the configured lease TTL, health endpoints must be
loopback-only and read-only, and restart/crash takeover must preserve
materialization and decision idempotency.

Phase 4G8 is the real long-horizon production validation gate. Use qualified
SWE-EVO-style software evolution tasks, not isolated single-issue patches, and
force observable process boundaries so completion cannot depend on one
continuous conversation. Formal runs must use real decision, compaction, and
Codex worker providers plus an independent official evaluator. Gold patches
and hidden tests must remain outside provider/worker context, evaluator results
must enter through task/run/receipt evidence rather than direct DB mutation,
and implementation self-verification must never satisfy a verifier-required
goal item. Independent verification must carry fixed-target producer/session
provenance and be enforced by the local completion reducer; known verifier
constraints must not depend on the Decision Provider deciding to add a node.
Do not force SWE-EVO tasks to naturally produce structure requests, graph
expansion, or first-pass evaluator failures. Cover those branches with
controlled integration cases and apply conditional assertions when they occur
in real tasks. Report Runtime Validation separately from End-to-End Capability
Validation: task-quality failure may coexist with runtime-correct evidence, but
all three small/medium/large tasks must still be officially resolved before
claiming the production capability baseline or beginning Phase 4H.

Phase 4G11 is the closed-loop runtime coordination stage. Workers still may
not communicate peer-to-peer or mutate the durable graph, but a nonterminal
worker may yield a canonical coordination checkpoint at a cooperative safe
point. Cross-node effects must flow through DB-backed global state, a validated
Decision Provider control proposal, a durable directive mailbox, same-session
resume, and explicit directive acknowledgment. Do not treat ordinary progress,
heartbeats, tool calls, or test completion as coordination events. Do not claim
that a directive changed execution until the target materialization has
acknowledged it. Mid-turn message injection and forced interruption are not
Phase 4G11 MVP requirements.

Phase 4G12 is the evidence-driven dynamic graph mutation stage. It extends a
coordination epoch only when a real worker checkpoint exposes a structured
durable responsibility candidate. The worker candidate cannot create nodes,
grant capabilities, update ledger facts, or mark goal completion. A valid
expansion must atomically create isolated child work, attach it to an existing
integration owner, and route all current `waiting_coordination`
responsibilities. More nodes are not themselves a success condition.

Phase 4G13 is the natural discovery and coordination cost-control stage. It
removes forced child-first checkpoints, allows terminal receipts to carry
non-authoritative responsibility candidates, requires exact candidate
resolution, and keeps local reducer transitions provider-free. The real Medium
comparison must not reveal candidate keys, file scopes, hidden test patches, or
gold changes to workers. A one-shot isolated acceptance suite may score each
arm after execution but must not feed an evaluator repair loop.

Phase 4G14 is the durable contribution handoff stage. Separate deterministic
attempt-patch capture from semantic receipt validation, keep quarantined
artifacts non-authoritative, promote only the exact captured attempt after a
valid child receipt, and reserve contribution classification for integration
owners. Do not rely on prompt wording alone for goal, directive, or artifact
identity; constrain current IDs from DB facts and return field-level validation
errors. A receipt protocol defect must never force implementation reexecution
or discard a completed isolated patch.

For this branch, do not routinely rebase `main`, and do not restore the old
oversized session `019e497b-56e0-7bb0-a357-0db06954ae4d` as implementation
context.
