# Hermes Kanban Runtime Kernel Phase 1 实现计划

本文档只定义第一阶段可落地闭环，不再扩展长期架构。长期原则以 `docs/kanban-runtime-kernel-design.md` 为准；本文件的作用是把第一批代码、schema、测试和明确排除项固定下来，避免实现时退化成普通 graph runner 或旧 Orchestra phase loop。

## Phase 1 目标

第一阶段要证明一个最小 goal-driven runtime 可以在现有 Hermes Kanban 上跑通：创建 runtime job 后生成 goal contract、goal items、decision session 和初始 analysis node；analysis node 可以物化成 Kanban task；fake worker evidence 可以被 ingest 到 node state、event log 和 progress ledger；本地 reducer 可以推导 goal gap、readiness、liveness 和 completion；deterministic fake decision provider 可以基于 DB-derived delta 返回 graph patch；validator 可以校验并应用 patch；最终只有 required goal items 被 ledger evidence 满足后，job 才由本地规则进入 done。

Phase 1 不追求真实智能，不接真实 LLM，不做 dashboard，不做 daemon，不实现完整 goal compiler。它只证明 DB 事实源、goal contract、progress ledger、reducer、patch validator、decision delta、Kanban materialization 这条链路成立。

## 必须保持的不变量

DB 是唯一权威事实源。decision session 只保存非权威推理上下文；第一阶段可以把 session 存成 DB 行和 JSON 字段，不需要接 provider 侧 session API。

goal contract 高于 execution graph。graph 跑完不等于 job done；graph 没有 runnable node 也不等于 blocked。job done 只能来自 progress ledger 和本地 completion rule。

reducer 拥有调度主权。LLM 或 fake provider 不能 release node、不能直接 complete job、不能自由 mark blocked。节点 ready、job state、goal gaps、liveness violation 和 completion 都由本地规则推导。

所有新 node 必须关联 `goal_item_keys`、`gap_keys` 或 `human_gate_reason`。没有目标关联的 patch 必须被 validator 拒绝。

## 文件和模块

新增模块：`hermes_cli/kanban_runtime_kernel.py`。

第一阶段所有 runtime kernel 代码先集中在这个模块内，避免过早拆分。模块可以包含 schema DDL、dataclass、patch validator、reducer、fake provider、helper API。后续当代码变大时再拆成 `kanban_runtime_schema.py`、`kanban_runtime_patch.py`、`kanban_runtime_reducer.py`。

新增测试文件：`tests/hermes_cli/test_kanban_runtime_kernel.py`。

第一批测试只使用 sqlite 临时 DB、deterministic fake evidence 和 fake decision provider。不依赖真实 Codex、Claude Code、网络、dashboard、browser smoke 或外部 worker 进程。

新增文档：本文件。实现过程中如果 Phase 1 范围变化，应先更新本文件，再写代码。

## Schema v1

Phase 1 使用独立 runtime 表，和现有 Kanban 表通过 `root_task_id`、`task_id`、`run_id`、`board` 关联。不修改旧 Orchestra 表，不复用 `kanban_orchestra.py`。

`runtime_jobs`：
`id TEXT PRIMARY KEY`、`root_task_id TEXT`、`board TEXT`、`state TEXT NOT NULL`、`objective TEXT NOT NULL`、`workspace_path TEXT`、`decision_profile TEXT`、`active_milestone_key TEXT`、`graph_revision INTEGER NOT NULL DEFAULT 0`、`advance_lock TEXT`、`claim_expires_at INTEGER`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`、`updated_at INTEGER NOT NULL`。

`goal_contracts`：
`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`objective TEXT NOT NULL`、`version INTEGER NOT NULL DEFAULT 1`、`state TEXT NOT NULL`、`constraints_json TEXT NOT NULL DEFAULT '{}'`、`defaults_policy_json TEXT NOT NULL DEFAULT '{}'`、`human_required_conditions_json TEXT NOT NULL DEFAULT '{}'`、`completion_policy_json TEXT NOT NULL DEFAULT '{}'`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`、`updated_at INTEGER NOT NULL`。

`goal_items`：
`id TEXT PRIMARY KEY`、`contract_id TEXT NOT NULL`、`item_key TEXT NOT NULL`、`description TEXT NOT NULL`、`required INTEGER NOT NULL DEFAULT 1`、`acceptance_criteria_json TEXT NOT NULL DEFAULT '{}'`、`evidence_requirements_json TEXT NOT NULL DEFAULT '{}'`、`verifier_required INTEGER NOT NULL DEFAULT 0`、`state TEXT NOT NULL`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`、`updated_at INTEGER NOT NULL`。唯一约束：`(contract_id, item_key)`。

`execution_nodes`：
`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`node_key TEXT NOT NULL`、`node_type TEXT NOT NULL`、`state TEXT NOT NULL`、`title TEXT NOT NULL`、`description TEXT NOT NULL`、`assignee TEXT`、`latest_task_id TEXT`、`latest_run_id INTEGER`、`input_summary TEXT`、`output_summary TEXT`、`assumptions_json TEXT NOT NULL DEFAULT '{}'`、`constraints_json TEXT NOT NULL DEFAULT '{}'`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`、`updated_at INTEGER NOT NULL`、`started_at INTEGER`、`completed_at INTEGER`。唯一约束：`(job_id, node_key)`。

`execution_dependencies`：
`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`from_node_id TEXT NOT NULL`、`to_node_id TEXT NOT NULL`、`dependency_type TEXT NOT NULL`、`required INTEGER NOT NULL DEFAULT 1`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`。唯一约束：`(job_id, from_node_id, to_node_id, dependency_type)`。

`node_relations`：
`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`from_node_id TEXT NOT NULL`、`to_node_id TEXT NOT NULL`、`relation_type TEXT NOT NULL`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`。唯一约束：`(job_id, from_node_id, to_node_id, relation_type)`。

`node_materializations`：
`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`node_id TEXT NOT NULL`、`attempt INTEGER NOT NULL`、`task_id TEXT NOT NULL`、`run_id INTEGER`、`worker_lane TEXT`、`status TEXT NOT NULL`、`created_at INTEGER NOT NULL`、`started_at INTEGER`、`completed_at INTEGER`、`terminal_event_id INTEGER`、`metadata_json TEXT NOT NULL DEFAULT '{}'`。唯一约束：`(node_id, attempt)` 和 `(task_id)`。

`progress_ledger`：
`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`contract_id TEXT NOT NULL`、`goal_item_id TEXT NOT NULL`、`node_id TEXT`、`artifact_id TEXT`、`verifier_node_id TEXT`、`evidence_ref TEXT`、`satisfaction TEXT NOT NULL`、`verification_state TEXT NOT NULL`、`confidence REAL`、`summary TEXT NOT NULL`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`。

`goal_gaps`：
第一阶段可以持久化，便于测试和 status 输出。字段为 `id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`goal_item_id TEXT`、`gap_key TEXT NOT NULL`、`gap_type TEXT NOT NULL`、`state TEXT NOT NULL`、`summary TEXT NOT NULL`、`evidence_ref TEXT`、`last_attempt_node_id TEXT`、`attempt_count INTEGER NOT NULL DEFAULT 0`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`、`updated_at INTEGER NOT NULL`。唯一约束：`(job_id, gap_key)`。

`execution_events`：
`id INTEGER PRIMARY KEY AUTOINCREMENT`、`job_id TEXT NOT NULL`、`node_id TEXT`、`task_id TEXT`、`run_id INTEGER`、`event_type TEXT NOT NULL`、`payload_json TEXT NOT NULL DEFAULT '{}'`、`source TEXT NOT NULL`、`source_event_id INTEGER`、`graph_revision INTEGER NOT NULL`、`created_at INTEGER NOT NULL`。

`graph_patches`：
`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`decision_id TEXT`、`base_revision INTEGER NOT NULL`、`applied_revision INTEGER`、`patch_json TEXT NOT NULL`、`status TEXT NOT NULL`、`reject_reason TEXT`、`created_at INTEGER NOT NULL`、`applied_at INTEGER`。

`kernel_decisions`：
`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`trigger_event_id INTEGER`、`db_revision INTEGER NOT NULL`、`decision_session_id TEXT`、`delta_json TEXT NOT NULL`、`decision_json TEXT`、`model TEXT`、`status TEXT NOT NULL`、`validator_result_json TEXT NOT NULL DEFAULT '{}'`、`error TEXT`、`created_at INTEGER NOT NULL`、`completed_at INTEGER`。

`decision_sessions`：
`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`profile TEXT`、`provider TEXT`、`model TEXT`、`state TEXT NOT NULL`、`stable_prefix_hash TEXT`、`session_ref TEXT`、`transcript_ref TEXT`、`last_appended_event_id INTEGER`、`last_checkpoint_revision INTEGER`、`context_state_json TEXT NOT NULL DEFAULT '{}'`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`、`updated_at INTEGER NOT NULL`。

`node_artifacts`：
Phase 1 可以先建表但只做最小引用。字段为 `id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`node_id TEXT`、`artifact_type TEXT NOT NULL`、`path_or_ref TEXT NOT NULL`、`summary TEXT NOT NULL`、`metadata_json TEXT NOT NULL DEFAULT '{}'`、`created_at INTEGER NOT NULL`。

## Public API v1

`ensure_runtime_schema(conn)`：创建所有 runtime 表和索引，可重复调用。

`create_runtime_job(conn, root_task_id, objective, board=None, workspace_path=None, goal_items=None)`：创建 job、goal contract、goal items、decision session、初始 analysis node 和 `job_created`/`goal_contract_created`/`node_created` events。若 `goal_items` 为空，Phase 1 生成一个最小 required item：`initial-runtime-result`，描述为“produce verified evidence for the requested objective”。这不是长期 goal compiler，只是测试闭环默认值。

`status_runtime_job(conn, job_id)`：返回 dict，至少包含 job、goal_contract、goal_items、progress_ledger、goal_gaps、nodes、dependencies、relations、materializations、recent_events、decisions、patches。

`advance_runtime_job(conn, job_id, board=None, create_tasks=True, decision_provider=None, max_patches=1)`：执行一次 kernel tick，返回 `AdvanceResult`。它必须可重复调用并保持幂等。

`apply_graph_patch(conn, job_id, patch, decision_id=None)`：校验 patch、写 graph_patches、应用结构变更、递增 graph_revision、写 patch_applied 或 patch_rejected event。

`build_decision_delta(conn, job_id, trigger_event_id=None)`：构造追加到 decision session 的 DB-derived delta。它不是完整 snapshot。

`append_decision_delta(conn, decision_session_id, delta, event_id=None)`：更新 session 的 `context_state_json`、`last_appended_event_id` 和 `updated_at`。Phase 1 不需要真实 transcript 文件。

`ingest_runtime_node_evidence(conn, node_id, board=None)`：读取已物化 Kanban task 的 `task_progress_snapshot()`，识别 terminal verdict，更新 node、events、artifacts、assumptions 和 progress ledger。

`update_progress_ledger(conn, node_id, evidence)`：把 worker evidence 中的 `claimed_goal_items`、`partial_goal_items`、`unmet_goal_items` 映射到 ledger 和 goal item state。

`detect_goal_gaps(conn, job_id)`：从 goal contract、goal items、progress ledger、node states 和 current graph 推导 gaps。

`reduce_runtime_job(conn, job_id)`：本地 reducer，负责 readiness、job state、goal gaps、synthetic audit、liveness 和 completion。

## Patch schema v1

所有 patch 必须包含 `schema="runtime_graph_patch_v1"`、`expected_revision`、`rationale_summary`、`ops`。

允许的 op 只有：

`create_node`：必填 `node_key`、`node_type`、`title`、`description`，且必须至少有 `goal_item_keys`、`gap_keys` 或 `human_gate_reason`。可选 `assignee`、`constraints`、`depends_on`。

`add_dependency`：必填 `from_node_key`、`to_node_key`，可选 `dependency_type`，默认 `depends_on`。只写 `execution_dependencies`。

`insert_verifier`：必填 `target_node_key` 或 `target_goal_item_key`、`verifier_node_key`、`title`、`goal_item_keys` 或 `gap_keys`。它创建 verification node，写调度依赖，并写 `node_relations.verifies`。

`request_human`：必填 `node_key`、`question`、`decision_type`、`why_user_required`、`default_recommendation`、`goal_item_keys` 或 `gap_keys`。只允许在人类策略确实要求时通过 validator。

`propose_blocked`：必填 `target`、`blocker_type`、`reason`、`evidence_ref`。`blocker_type` 只允许 `missing_secret`、`external_permission`、`destructive_change_needs_approval`、`unavailable_dependency`、`system_error`、`policy_violation`。它不直接写 blocked 终态，只生成可由 reducer 采纳的结构事实。

明确不支持 `release_node`。明确不支持 provider 直接 `complete_job`。如果测试里出现这些 op，validator 必须拒绝。

## Reducer v1

reducer 输入是 DB 当前状态，不接收 LLM 输出以外的隐式上下文。

第一步更新 readiness：所有 `planned` 或 `waiting_dependency` 节点，如果 required `execution_dependencies` 的上游节点都处于 `succeeded`，且没有 active human gate 或 blocked policy，则进入 `ready`。

第二步更新 job state：存在 running/materialized node 时为 `waiting_worker`；存在 ready 未物化 node 时为 `active`；存在 active human gate 时为 `waiting_human`；存在 goal gap 且需要结构决策时为 `waiting_decision`；所有 required goal items satisfied 且无 active blocker/human/running required node 时为 `done`。

第三步更新 goal gaps：required item 没有 ledger evidence 生成 `missing_evidence`；ledger partial 生成 `partial_satisfaction`；ledger unverified 生成 `unverified_evidence`；verifier failed 生成 `failed_verifier`；无 ready/running 且 job 未 done 生成 `no_runnable_graph`。

第四步生成 synthetic events：`goal_gap_detected`、`decision_requested`、`structure_audit_requested`、`liveness_violation`。`node_completed`、`node_failed` 和 `node_blocked` 只是 reducer 输入，不直接调用 decision provider。

第五步执行 completion rule：只有 required goal items 都 satisfied，required verifier 成功，没有 unresolved hard constraint，没有 active human gate，没有 active blocker，没有 running required node，没有 contradicted ledger entry，job 才能进入 `done`。

## Kanban materialization v1

materialization 只处理 `ready` 且没有 active materialization 的 node。创建 Kanban task 时使用现有 `hermes_cli.kanban_db.create_task()`，task body 是 worker context，不是完整历史。

task body 必须包含 root objective、node title/description、goal item/gap、success evidence contract、direct dependency summaries、constraints、expected receipt schema 和机器可读 footer：`runtime_job_id`、`execution_node_id`、`node_key`、`node_type`、`node_materialization_id`。

materialization 成功后创建 `node_materializations` 行，记录 `node_materialized` event，写回 `execution_nodes.latest_task_id/latest_run_id`。如果 Kanban task 创建成功但 runtime 写回失败，第一阶段必须通过事务或幂等补偿避免重复 task。最保守策略是先在 runtime 表保留 materialization intent，再创建 task，再补 task_id；测试必须覆盖重复 advance 不创建重复 task。

## Evidence receipt v1

Phase 1 fake evidence 使用 dict，不要求真实 worker markdown 解析。字段至少包括：

`verdict`：`succeeded`、`failed`、`blocked`、`waiting_human`、`uncertain`。

`summary`：一行结构化摘要。

`claimed_goal_items`：声称完全满足的 goal item keys。

`partial_goal_items`：部分满足的 goal item keys。

`unmet_goal_items`：仍未满足的 goal item keys。

`verification`：包含 `commands`、`passed`、`summary`。

`artifacts`：包含 `path_or_ref`、`artifact_type`、`summary`。

`new_constraints`、`human_gate_suggestions`、`active_assumptions`、`decisions_made`、`rejected_approaches`、`known_failure_boundaries`、`open_questions`、`risk_notes`。

ingest 不能因为 node succeeded 就自动满足 goal item；必须看 `claimed_goal_items` 和 verifier/evidence requirements。

## Deterministic fake provider

Phase 1 fake provider 是测试 fixture，不是默认 workflow。它只根据 delta 里的 gap 做确定性 patch。

建议实现 `fixture_decision_provider(session, delta)`：

如果 delta 含 `missing_evidence` gap，返回一个 `create_node` implementation node，关联该 goal item/gap。

如果 implementation evidence 已 self_reported 但 goal item 仍 unverified，返回 `insert_verifier`。

如果 verifier failed，返回 debug node 或 verifier retry node。

如果 no runnable graph 且仍有 unmet required item，返回一个服务于该 gap 的 node；不能返回 noop。

fake provider 不能硬编码所有任务都走固定 `analysis -> implementation -> verification`。测试可以使用这个三步 fixture，但断言结构来自 gap delta 和 patch，而不是来自 node_type lifecycle 规则。

## 测试清单

第一批 pytest：

`test_schema_initializes_runtime_tables`：`ensure_runtime_schema()` 创建所有表和关键唯一约束。

`test_create_runtime_job_creates_contract_session_and_initial_node`：创建 job 后存在 goal contract、goal item、decision session、analysis node 和 events。

`test_patch_rejects_release_node_and_direct_complete`：validator 拒绝 `release_node` 和直接 complete。

`test_patch_rejects_node_without_goal_or_gap_linkage`：没有 goal/gap/human linkage 的 create_node 被拒绝。

`test_patch_rejects_stale_revision`：`expected_revision` 不匹配时拒绝。

`test_dependency_cycle_rejected`：调度依赖成环时拒绝。

`test_reducer_computes_ready_without_release_node`：依赖满足后 reducer 自动把下游 node 设为 ready。

`test_materialization_is_idempotent`：同一个 ready node 连续 advance 不创建重复 Kanban task。

`test_fake_evidence_updates_progress_ledger`：fake evidence 的 claimed/partial/unmet goal items 正确进入 ledger。

`test_node_completed_does_not_directly_call_provider`：node completed 后先 reducer；只有 reducer 写 `decision_requested` 才调用 provider。

`test_no_runnable_unmet_goal_records_liveness_violation`：无 ready/running 且 goal 未满足时记录 liveness violation 或 decision_requested，不能静默停。

`test_done_requires_required_goal_items_satisfied`：node succeeded 但 ledger 不满足时 job 不能 done；ledger 满足且 verifier 通过后才能 done。

`test_propose_blocked_requires_machine_blocker_type`：非法 blocker_type 被拒绝，合法 propose 也不能直接终止 job。

`test_request_human_requires_policy_reason`：request_human 必须有 decision_type、why_user_required、default_recommendation 和 goal/gap linkage。

## Phase 1 明确排除项

不接真实 LLM provider。

不实现 provider 侧 session API，只保存本地 `decision_sessions` 和 delta。

不实现 dashboard 和 `/runtime/jobs` HTTP API。

不实现常驻 runtime daemon。

不实现完整自然语言 goal compiler。

不迁移旧 Orchestra 前端。

不跑真实 Codex smoke 作为单测前提。

不实现复杂 retry 策略，只保证 materialization 幂等和 patch 幂等。

不实现完整 checkpoint compaction，只保留接口和最小 session state。

## 完成定义

Phase 1 完成必须满足三个条件。

第一，`pytest tests/hermes_cli/test_kanban_runtime_kernel.py` 全部通过，且测试覆盖本文件列出的核心不变量。

第二，用 fake provider 和 fake evidence 可以跑通 create job -> materialize analysis -> ingest evidence -> detect gap -> apply patch -> materialize implementation -> ingest implementation evidence -> materialize verifier -> ingest verifier evidence -> local done 的闭环。

第三，文档里的排除项没有被偷偷实现成核心依赖。真实 LLM、dashboard、daemon、Codex lane 都可以后续接入，但不能成为 Phase 1 单测通过的前提。
