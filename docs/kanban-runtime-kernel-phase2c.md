# Hermes Kanban Runtime Kernel Phase 2C 实现计划

本文档定义 Phase 2C 的范围。Phase 2B 已经补齐 decision provider/session 基础：provider input 可以从 DB-derived delta、decision session 和 checkpoint 渲染出来，provider output 进入 kernel 前必须经过严格 JSON patch 解析，parse failure、patch rejected 和 stale revision 都能被审计记录，且不依赖真实 LLM、网络、dashboard 或 daemon。

Phase 2C 的目标不是继续扩展 provider 能力，也不是接真实模型，而是把 runtime kernel 的本地目标推进语义补硬。换句话说，Phase 2B 解决“模型如何提出 patch proposal 才可控”，Phase 2C 解决“即使没有模型智能，runtime 自己也能判断目标差距、推进状态、停滞和合法停止条件”。这一步完成前，不应进入真实 LLM provider 或 dashboard 阶段。

长期架构仍以 `docs/kanban-runtime-kernel-design.md` 为准。Phase 1、Phase 2A 和 Phase 2B 的不变量继续有效：DB 是权威事实源，decision session 是非权威推理上下文，goal contract/progress ledger 定义完成，reducer 拥有 readiness、job state、goal gaps、liveness、synthetic audit 和 completion，decision provider 只能提出 graph patch，不能 release node、complete job、直接写 DB 或自由 mark blocked。

## Phase 2C 目标

第一，强化 goal-driven reducer。`reduce_runtime_job()` 不能只是更新节点 readiness 和做简单 completion；它必须明确推导 goal item satisfaction、open goal gaps、active frontier、legal waiting state、liveness violation 和 decision_requested。node completed、node failed、node blocked 仍然只是 reducer 输入，不直接等于 provider 调用。

第二，强化 progress ledger 语义。worker evidence ingest 后，系统必须把 evidence 映射到 goal item，而不是只保存 node summary。ledger entry 要能表达 full、partial、none、contradicted 这类满足程度，以及 verified、unverified、failed、not_required 这类验证状态。completion rule 只能基于 ledger 和 verifier evidence 判定 job done。

第三，强化 goal gap detector。`detect_goal_gaps()` 要从 goal contract、goal items、progress ledger、node states、open human gates 和 graph frontier 推导 gap，而不是只对缺失 evidence 做单一判断。Phase 2C 至少要区分 missing_evidence、needs_verification、contradicted_evidence、failed_required_node、blocked_by_human_gate、no_runnable_for_open_goal、stale_or_no_progress。

第四，强化 liveness invariant。只要 job 未完成，且没有 running node、ready node、active human gate、pending decision 或合法 blocked state，runtime 就不能静默停止。它必须记录 `liveness_violation` 或 `decision_requested`，并使下一次 `advance_runtime_job()` 能进入 gap resolution。

第五，引入最小 anti-stuck policy。长期任务最常见的问题不是显式失败，而是重复无效推进。Phase 2C 要用本地规则检测同一 gap 多轮无新增 ledger evidence、同类 node 连续失败、decision 连续 noop/rejected、worker 多次 uncertain、active milestone 超过预算无满足项等状态，并生成 `structure_audit_requested` 或 `strategy_update_requested` 事件。

第六，收紧 human gate policy。human gate 是受控阻塞，不是模型或 runtime 逃避复杂任务的出口。Phase 2C 要把“什么时候允许 request_human”写成本地 policy，并在 validator 或 reducer 中拒绝缺少机器可读原因的 human gate。普通工程默认选择应继续推进并记录 rationale；只有外部费用、凭证、权限、破坏性变更、用户偏好或高影响架构路线无法默认时，才允许等待用户。

第七，扩展多节点场景测试。Phase 2C 的测试不能只证明 analysis -> implementation -> verification fixture 可以跑通；必须覆盖并行节点、join point、failed verifier、stale patch、重复 materialization 防护、goal 未完成但图耗尽、human gate policy 和 anti-stuck synthetic event。

## 明确非目标

不接真实 LLM provider。Phase 2C 可以继续使用 replay provider、recording provider 或 deterministic fixture provider，但真实 OpenAI/Anthropic/Codex/Claude Code 调用不是完成前提。

不做 dashboard UI。可以补 CLI/API JSON 字段，方便观察 gap、ledger、liveness 和 synthetic events，但不迁移前端页面。

不做常驻 runtime daemon。supervisor 仍然是 CLI/API 触发的 bounded loop。Phase 2C 要保证 bounded loop 每次退出都有合法原因，但不需要实现后台常驻进程。

不扩展旧 Orchestra。不得引入 planner/coder/reviewer/tester 固定角色，也不得把 node_type 写成 lifecycle phase。

不放宽 validator。Phase 2C 不能为了让测试容易通过而允许 `release_node`、`complete_job`、无 goal linkage 的 `create_node`、无 `expected_revision` 的 patch 或自由文本 blocked state。

不把 `blocked` 当作普通停止状态。只有存在明确外部资源、权限、secret、破坏性变更、策略违规、不可用依赖或系统错误时，才能进入合法 blocked。

## 当前基线

当前代码已经具备以下基础：

`kanban_runtime_kernel.py` 中已有 runtime schema、goal contract、goal items、progress ledger、goal gaps、execution nodes、execution events、graph patches、decision sessions、decision checkpoints、node materializations、job create/status/list/advance、evidence ingest、patch validator、basic reducer 和 materialization。

`kanban_runtime_decision.py` 中已有 decision provider request、prompt rendering、checkpoint creation、strict provider patch parsing、replay provider 和 recording provider。

`hermes kanban runtime ...` 已经支持 create、promote、status、list、advance、decision、checkpoint 和 prompt 等 control-plane 命令。

Phase 2C 不应重写这些基础。实现应该优先在现有 helper 周围补充更强的不变量和测试，只有当现有字段无法表达目标推进语义时才扩 schema。

## Reducer Contract

`reduce_runtime_job(conn, job_id)` 在 Phase 2C 后应当稳定承担以下职责：

第一，读取 authoritative DB state，包括 job、goal contract、goal items、progress ledger、execution nodes、node materializations、open gaps、execution events、human gates、graph revision 和最近 decision 结果。

第二，重算节点 readiness。只有调度依赖满足、节点处于 planned/waiting_dependency、没有 active materialization、没有被 superseded、没有违反 resource policy 时，节点才能进入 ready。decision provider 不能通过 patch 放行节点。

第三，重算 goal gaps。gap detector 必须返回稳定、可排序、可审计的 gap rows。gap key 必须稳定，避免每轮 advance 生成新的重复 gap。

第四，重算 job state。job state 是 materialized execution view，不是 graph done 的简单映射。它应优先表达 done、waiting_worker、waiting_human、needs_decision、blocked、running 或 ready 等可恢复状态。

第五，生成 synthetic events。reducer 可以写入 `goal_gap_detected`、`decision_requested`、`structure_audit_requested`、`strategy_update_requested`、`liveness_violation`、`completion_ready` 等事件，但必须做到幂等，不能每次 status 查询都重复写事件。

第六，执行 completion rule。只有 required goal items 都被足够 evidence 支持、required verifier 成功、没有 unresolved hard constraint、没有 active human gate、没有 failed required verifier、没有 running required node、没有 contradicted ledger entry 时，job 才能由本地规则进入 done。

## Goal Gap Types

Phase 2C 至少支持这些 gap type：

`missing_evidence`：required goal item 没有任何 usable ledger evidence。

`partial_evidence`：goal item 有 ledger evidence，但 satisfaction 仍是 partial，缺少必要 artifact、说明、实现范围或验收证据。

`needs_verification`：goal item 有实现 evidence，但 verifier_required 为 true 且没有 verified ledger entry。

`verification_failed`：相关 verifier node 或 verification evidence failed，目标不能完成。

`contradicted_evidence`：ledger 中存在 contradicted evidence，必须先 debug、supersede、waive 或请求人工。

`failed_required_node`：某个服务 required goal item 的节点失败，且当前没有替代路径或 debug/fix node。

`blocked_by_human_gate`：目标推进需要合法 human gate 的回答。

`no_runnable_for_open_goal`：仍有 open required goal item，但当前 graph 没有 ready/running node 能推进它。

`stale_or_no_progress`：同一 gap 多轮 advance 没有新增 ledger evidence 或有效 patch。

`strategy_exhausted`：同一 gap 的 retry/debug/research 路径超过 policy budget，需要 strategy update 或 human gate。

这些 gap type 不要求一次性实现复杂智能，但 schema、状态和测试要允许后续扩展。所有 gap 都必须带 `goal_item_key` 或 `gap_key`，并说明它为什么影响 completion。

## Progress Ledger Contract

worker evidence 进入 runtime 后，Phase 2C 的 ingest 应尽量提取以下字段：

`claimed_goal_item_keys`：worker 声称推进的 goal items。

`satisfaction`：full、partial、none、contradicted。

`verification_state`：verified、unverified、failed、not_required。

`evidence_summary`：短摘要，说明 evidence 为什么支持或不支持 goal item。

`artifact_refs`：代码、测试结果、日志、数据、文档或工具输出引用。

`verification_refs`：验证命令、测试输出、manual check 或 verifier node 引用。

`remaining_gaps`：worker 明确指出仍缺的目标证据。

`new_constraints`：执行中发现的新硬约束或外部依赖。

`active_assumptions`：当前仍有效的局部假设。

`rejected_approaches`：已尝试并排除的路线。

`known_failure_boundaries`：失败边界，例如某 API 不可用、某 schema 不兼容、某测试环境缺依赖。

Phase 2C 不要求 worker 一定返回所有字段。缺失字段时 ingest 必须保守处理：可以更新 node state，但不能把 goal item 标成 satisfied；可以生成 missing_evidence 或 needs_verification gap。

## Decision Trigger Contract

结构性事件和 provider 调用必须分开。

`node_completed`、`node_failed`、`node_uncertain`、`node_blocked`、`human_decision_received`、`patch_applied`、`patch_rejected` 都是 reducer 输入。它们本身不等于“立即调用 provider”。

只有 reducer 判断出现以下情况时，才应写入 `decision_requested` 并让 `advance_runtime_job()` 调 provider：

目标未完成，且没有 ready/running node 可以推进 open gap。

verifier failed，需要选择 debug、retry、split、supersede 或 request_human。

多个并行分支到达 join point，需要创建 merge/verifier/follow-up node。

同一 gap 多轮无 progress，anti-stuck policy 要求 strategy update。

patch rejected 或 parse failed 后，当前 graph 无法靠本地规则继续推进。

manual operator request 明确要求结构调整。

如果本地 reducer 可以直接释放已有 verifier node、更新 ledger、完成 job 或等待 worker，就不应该调用 provider。

## Human Gate Policy

Phase 2C 应定义最小 `human_gate_policy`，可以先存在于 goal contract metadata 或 runtime policy helper 中。

允许 human gate 的原因包括：

`missing_secret`：需要用户提供外部凭证、token、SSH key、API key。

`external_cost`：需要启用付费 API、云资源或明显成本。

`destructive_change`：可能删除数据、迁移 schema、覆盖用户文件或破坏兼容性。

`permission_required`：需要系统权限、网络权限、远端仓库权限或用户授权。

`product_preference`：多个高影响产品/架构方向都合理，且 defaults policy 无法选择。

`legal_or_policy`：存在合规、许可或用户政策边界。

不允许 human gate 的原因包括普通内部命名、目录组织、实现细节、测试框架选择、是否先 mock 后真实接入、局部 debug 路线。此类问题应由 runtime 按 defaults policy 推进，并记录 rationale。

`request_human` patch 或 human gate node 必须带 `decision_type`、`why_user_required`、`default_recommendation`、`options` 或 `requested_input_schema`。validator 应拒绝缺少机器可读原因的 human gate。

## Anti-Stuck Policy

Phase 2C 的 anti-stuck 不需要复杂模型判断，应先用可测试规则：

同一 open gap 的 `attempt_count` 超过阈值且没有新增 full/verified ledger evidence，生成 `stale_or_no_progress`。

同一 goal item 关联节点连续 failed 超过阈值，生成 `strategy_update_requested`。

连续 provider decisions 返回 noop、parse_failed 或 rejected_patch，且 graph revision 没变化，生成 `structure_audit_requested`。

worker 对同一 gap 多次返回 uncertain，生成 research/debug/human gate 候选，而不是继续相同节点。

active milestone 超过预算但没有任何 required goal item 状态改善，生成 `strategy_update_requested`。

这些事件仍然不能直接让 LLM 或 provider 控制状态。它们只是让 reducer 进入 gap resolution，并要求后续 patch 创建更小的 debug node、research node、verifier node、替代路径或合法 human gate。

## Schema/API 建议

优先复用现有表。若现有字段不足，可以小幅扩展：

`goal_gaps.metadata`：记录 `attempt_count`、`last_progress_event_id`、`last_evidence_at`、`stuck_reason`、`policy_budget`、`related_node_keys`。

`progress_ledger.metadata`：记录 `claimed_by_worker`、`artifact_refs`、`verification_refs`、`remaining_gaps`、`new_constraints`、`active_assumptions`、`rejected_approaches`、`known_failure_boundaries`。

`execution_events.payload`：为 synthetic events 记录 `gap_key`、`goal_item_key`、`reason`、`policy`、`previous_event_id`，保证事件可审计且幂等。

`runtime_jobs.metadata`：记录 `active_milestone_key`、`last_decision_revision`、`last_liveness_check_revision`、`anti_stuck_policy`、`human_gate_policy`。

如果需要新增 helper，建议保持在 `kanban_runtime_kernel.py` 内，除非它明显属于 provider/session 层：

`classify_goal_item_state(conn, goal_item_id)`：从 ledger 推导 goal item 状态。

`summarize_active_frontier(conn, job_id)`：返回 ready/running/blocked/terminal frontier。

`detect_liveness_violation(conn, job_id, gaps, frontier)`：返回是否存在非法 idle。

`detect_stagnation(conn, job_id, gaps)`：返回 anti-stuck synthetic events。

`validate_human_gate_request(op, policy)`：校验 request_human 或 human_gate node 是否符合 policy。

`record_synthetic_event_once(conn, job_id, event_type, key, payload)`：避免 reducer 重复写同类事件。

## CLI/API 可观测性

Phase 2C 可以扩展现有 runtime status JSON，但不做 UI：

`hermes kanban runtime status <job_id> --json` 应能展示 open goal gaps、ledger summary、frontier summary、pending decision reason、liveness state 和 active human gate。

`hermes kanban runtime advance <job_id> --json` 应在结果里说明退出原因，例如 done、waiting_worker、waiting_human、needs_decision、max_steps_reached、blocked、liveness_violation_recorded。

可以增加 `runtime gaps <job_id> --json` 或在 status 中复用 `goal_gaps`，但不要过早增加大量 CLI 子命令。

## 测试清单

`test_completed_node_does_not_always_trigger_decision`：node completed 后，如果本地 reducer 只是释放已有 verifier node，不调用 provider。

`test_unmet_goal_without_runnable_node_requests_decision`：required goal 未满足且没有 ready/running/human 时，记录 `decision_requested` 或 `liveness_violation`，不能静默停。

`test_goal_item_requires_verified_ledger_for_completion`：worker 声称完成但没有 verified ledger evidence 时，job 不进入 done。

`test_partial_ledger_creates_partial_evidence_gap`：partial evidence 生成 partial_evidence 或 needs_verification gap。

`test_failed_verifier_creates_verification_failed_gap`：verifier failed 后 job 不 done，并请求 debug/split/strategy decision。

`test_contradicted_ledger_blocks_completion`：contradicted evidence 阻止 completion，并生成 contradicted_evidence gap。

`test_parallel_branches_join_without_per_node_decision_spam`：多个并行节点陆续完成时，不为每个 completed 都调用 provider；到 join point 后才 decision_requested。

`test_stale_gap_generates_structure_audit`：同一 gap 多轮无新增 evidence 时生成 structure_audit_requested 或 strategy_update_requested。

`test_rejected_patch_counts_toward_anti_stuck`：连续 rejected/noop patch 不改变 graph revision，并触发 anti-stuck event。

`test_human_gate_requires_policy_reason`：缺少合法 reason 的 request_human 被 validator 拒绝。

`test_human_gate_waiting_is_legal_liveness_state`：存在 active human gate 时，不记录非法 idle liveness violation。

`test_blocked_requires_machine_readable_blocker_type`：自由文本 blocked 或不合法 blocker_type 被拒绝或不能让 job 进入合法 blocked。

`test_ready_materialization_is_idempotent_under_two_advances`：同一个 ready node 在重复 advance 下不会重复创建 active materialization。

`test_stale_revision_patch_after_liveness_event_is_rejected`：基于旧 revision 的 patch 在 synthetic event 或 graph revision 变化后被拒绝。

## 完成定义

Phase 2C 完成必须满足：

第一，goal gap detector 不再只覆盖 missing evidence，而是能表达 verification、contradiction、failed required node、no runnable graph、human gate 和 no progress。

第二，reducer 能稳定输出 job 的合法等待状态和非法 idle 状态；目标未完成时不能因为当前 graph 耗尽而静默停止。

第三，progress ledger 参与 completion rule；没有足够 verified evidence 的 required goal item 不能让 job done。

第四，human gate 和 blocked state 受本地 policy 约束，不能成为 provider 或 fixture 的任意停止出口。

第五，anti-stuck synthetic events 有最小可测试实现，能检测重复无进展、连续 rejected/noop decision 或连续 uncertain/failed attempt。

第六，相关 CLI/API JSON 能展示 open gaps、ledger summary、frontier/liveness 状态，便于后续 dashboard 复用。

第七，测试全部使用 fake worker evidence、deterministic/replay provider 和本地 SQLite，不依赖真实 LLM、网络、dashboard、daemon 或真实 Codex/Claude Code。

Phase 2C 结束后，runtime kernel 才适合进入真实 provider 接入阶段。届时真实 LLM 只会接到更清晰的 goal gaps、frontier 和 decision delta，而不是被迫用 prompt 弥补本地 reducer、ledger、liveness 或 completion rule 的缺口。
