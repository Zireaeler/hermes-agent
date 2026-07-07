# Hermes Kanban Runtime Kernel 设计文档 v2

本文档是 `feature-kanban-runtime-kernel` 分支的 canonical design。实现、测试和后续阶段文档如果与本文档冲突，应先更新设计或停止实现并确认方向。

## 1. 背景

Hermes Kanban 已经具备任务看板、worker lane、worker dispatch、worker run、task event、worker evidence、dashboard 和 CLI/API 基础能力。旧 Orchestra 在这个执行底座上实现过 planner/coder/reviewer/tester 等角色式多 agent 编排，并证明了 Kanban 可以调起多个 worker、收集 evidence、推进 review/test/fix loop。

旧 Orchestra 的核心问题是它把任务推进方式固化成阶段式 workflow。典型路径是 planning、plan_review、implementation、review/test、fix、done/blocked，并围绕固定角色创建 invocation。这个结构可以证明多 worker 调度成立，但不适合作为长期通用 runtime，因为它提前预设了协作形态、阶段边界和角色分工。

新的 runtime kernel 目标不是继续增强旧 Orchestra，而是在 Kanban 执行层之上构建一个 goal-driven、event-sourced、可恢复、可审计、可长期推进复杂任务的执行运行时。

这个 runtime 不把 Codex、Claude Code 或任何具体 worker 当成架构中心。Codex/CC 是可替换的 execution backend。系统核心是外部事实状态、目标合同、执行图、决策会话、上下文压缩、图补丁校验和目标持续推进机制。

## 2. 设计目标

本系统的目标是让 Hermes 从“任务看板 + worker 调度”升级为“目标驱动的任务级执行 runtime”。

用户仍然面对主对话或主 agent。主 agent 不亲自长期执行复杂任务，而是将用户目标转换为 runtime job。runtime job 进入后台后，由 runtime kernel 维护目标、证据、执行图和决策上下文，并通过 Kanban worker lane 调用 Codex/CC、本地脚本、人工任务或其他 backend 执行局部节点。

系统必须支持复杂长期任务的持续推进。只要目标合同尚未被证据满足，且没有合法 human gate，系统就不应因为当前 execution graph 没有 ready node 而静默停止。当前图耗尽只能说明现有执行结构不足，runtime 必须进入 gap resolution 或 strategy update。

系统必须支持外部事实源。LLM 可以拥有长期推理上下文，但不能拥有事实状态，不能直接修改 execution graph，不能直接完成 job，不能绕过 validator。所有事实状态必须能从数据库恢复。

系统必须支持长上下文与前缀缓存。LLM 不应每次冷启动读取全量 snapshot。每个 runtime job 可以维护长期 decision session，用于保留推理连续性和缓存友好的稳定前缀。但 decision session 不是事实源，只是推理上下文。

系统必须支持 decision session compaction。复杂任务会产生很长的系统级调度对话，即 DB delta、LLM patch、validator result、graph/ledger/gap change 的连续记录。这个上下文必须像 Codex/CC loop 的 compact 一样周期性压缩。压缩后旧 transcript 原文退出 active LLM context，只作为归档审计材料保留。

## 3. 非目标

本系统第一阶段不追求“完全自主智能体社会”。它不要求 worker 之间直接通信，不要求子 agent 形成上下级关系，不要求 runtime 一开始就能自动学习最优 graph topology。

本系统不把 Markdown 作为事实源。Markdown 可以是 evidence、人读 handoff、artifact、audit document，但进入 runtime 决策的内容必须转成结构化 DB state、artifact summary、goal evidence 或 decision delta。

本系统不绕开 Kanban 直接管理 worker 进程。Kanban 继续负责真实 worker 生命周期、dispatch、lane、heartbeat、timeout、crash/retry、task event、run evidence 和 dashboard 基础可观测性。

本系统不把 worker receipt 当作上下文压缩。worker receipt 是节点交付契约，是 worker 在启动时就必须按 schema 返回的结构化产物。runtime compaction 的对象是 job 级 decision session transcript，而不是 worker log 或节点交付物。

## 4. 核心原则

### 4.1 DB 是唯一事实源

系统的事实连续性存在于数据库中，包括 runtime job、goal contract、progress ledger、execution graph、event log、artifact、graph patch、kernel decision、decision session segment 和 checkpoint。

LLM 的上下文可以连续，但不是事实源。若 LLM decision session 里的记忆与 DB 冲突，以 DB 为准。冲突会通过 validator 拒绝、patch_rejected event、correction delta 的形式反馈回 decision session。

### 4.2 Decision Session 是推理上下文

每个 runtime job 可以有一个长期 decision session。它用于保存 LLM 理解长期任务所需的上下文，例如目标解释、关键设计选择、已否定路径、validator 拒绝历史、human decision、active milestone、当前 goal gap、重要 artifact index。

Decision session 不拥有写权限。它只能用于生成 graph patch proposal。patch proposal 必须经本地 validator 用当前 DB revision 校验后才能落库。

### 4.3 Execution Graph 不是目标

Execution graph 是满足 goal contract 的临时执行结构。它可以扩展、拆分、替换、废弃、重构。系统不能因为当前 graph 跑完就认为目标完成。

目标是否完成由 goal contract 和 progress ledger 决定。

### 4.4 Goal Contract 是完成标准

用户自然语言 objective 必须规范化为 goal contract。Goal contract 描述任务完成时必须满足的可验证条件、硬约束、默认策略和需要 human gate 的边界。

所有 execution node 应尽量关联 goal item、goal gap 或 human gate reason。没有目标关联的 node 容易退化成模型“想做点什么”，应被 validator 拒绝或至少降级为需要人工确认。

### 4.5 Worker 是局部执行单元

Codex/CC、本地脚本、人工作业或其他 lane 都是 execution backend。worker 只执行一个 node 的局部任务，不持有全局控制权，不与其他 worker 直接通信，不直接修改 execution graph。

worker 通过结构化 receipt、artifact 和 evidence 回写 DB。协作通过 execution graph、artifact dependency 和 progress ledger 实现，而不是通过 agent-to-agent 对话实现。

### 4.6 Liveness 是 Runtime 不变量

如果 job 未完成，且没有 running node、ready node、active human gate、pending decision，则不是正常 idle，而是 liveness violation。runtime 必须触发 goal-gap decision 或 strategy update。

### 4.7 压缩必须替换活跃上下文

Decision session compaction 不是“在长对话后面追加 summary”。一旦 compaction 成功，旧 segment 原文必须退出 active LLM context，只能归档审计。新的 active segment 使用 stable runtime contract、current goal contract、validated checkpoint、short tail 和 current delta 继续。

## 5. 系统边界

### 5.1 Frontend / Main Agent

主对话 agent 是控制面，不是执行面。它可以创建 runtime job、查询 job 状态、解释当前 goal progress、execution graph 和 blockers、写入 human decision、触发一次 advance、启动或停止 bounded supervisor、请求 cancel 或 pause。

它不应直接修改 execution graph。若要影响结构，必须通过 graph patch API、human decision event 或 runtime command 进入同一套状态系统。

### 5.2 Runtime Kernel

Runtime kernel 是后台执行内核。它负责维护 runtime job、goal contract、progress ledger、execution graph、event log 和 decision session；调用 decision provider；校验 graph patch；物化 ready node 为 Kanban task；ingest worker evidence；检测 goal gap、liveness violation 和 anti-stuck；触发 human gate；触发 decision session compaction。

Runtime kernel 不直接执行代码，不直接跑测试，不直接管理外部 worker 进程。

### 5.3 Kanban Execution Layer

Kanban 负责实际执行生命周期，包括 tasks、task_runs、task_events、task_links、worker lanes、dispatch_once、dispatcher daemon、claim、spawn、heartbeat、timeout、stale reclaim、crash detection、worker evidence 和 task_progress_snapshot。

Runtime node 物化成 Kanban task 后，由 Kanban 执行系统处理 worker lifecycle。

### 5.4 Worker Backend

worker backend 包括 Codex、Claude Code、本地脚本、人工作业、未来其他 agent backend。它们只接收 node 局部上下文并返回结构化 receipt。

worker backend 可以在自身内部使用长上下文和上下文压缩。runtime kernel 不管理 worker 内部压缩，只要求最终 receipt 满足节点交付契约。

## 6. 运行模型

Runtime 的核心闭环不是“agent 持续思考”，而是：

1. DB state 变化。
2. 本地 reducer 更新 graph、ledger、frontier。
3. 若需要执行，则 materialize ready node。
4. 若需要结构决策，则追加 delta 到 decision session。
5. decision provider 返回 graph patch proposal。
6. validator 校验 patch。
7. patch 落库或拒绝。
8. 结果追加回 event log 和 decision session。
9. 循环继续。

系统既不是完全 stateless snapshot oracle，也不是单个长期负责人 agent。它是外部事实源 + 长期推理上下文 + 局部执行节点 + 本地校验器。

## 7. 核心数据模型

### 7.1 runtime_jobs

`runtime_jobs` 是复杂任务的根对象。建议字段：`id`、`root_task_id`、`board`、`state`、`objective`、`workspace_path`、`decision_profile`、`active_decision_session_id`、`active_segment_id`、`latest_checkpoint_id`、`goal_contract_revision`、`graph_revision`、`ledger_revision`、`liveness_state`、`metadata_json`、`created_at`、`updated_at`。

`state` 建议限制为 `active`、`waiting_worker`、`waiting_decision`、`waiting_human`、`blocked`、`done`、`cancelled`、`failed`。state 是 runtime projection，不是固定 phase。它由 nodes、goal contract、progress ledger、human gates 和 liveness reducer 推导。

### 7.2 goal_contracts

Goal contract 描述目标完成条件。建议字段：`id`、`job_id`、`revision`、`objective_summary`、`contract_json`、`default_policy_json`、`human_gate_policy_json`、`created_at`、`updated_at`。

`contract_json` 包含 goal items、硬约束、optional items、waiver 规则、completion policy 和 verification policy。

### 7.3 goal_items

每个 goal item 是可被 evidence 满足的目标条款。建议字段：`id`、`job_id`、`goal_key`、`title`、`description`、`required`、`state`、`satisfaction_rule_json`、`verification_required`、`risk_level`、`created_at`、`updated_at`。

`state` 建议为 `unmet`、`in_progress`、`partial`、`satisfied`、`blocked`、`waived`。

### 7.4 progress_ledger

Progress ledger 记录 evidence 如何支持 goal item。建议字段：`id`、`job_id`、`goal_item_id`、`node_id`、`artifact_id`、`event_id`、`verifier_node_id`、`evidence_type`、`verification_state`、`confidence`、`summary`、`metadata_json`、`created_at`、`updated_at`。

`verification_state` 可为 `self_reported`、`partial`、`verified`、`failed`、`waived`。Completion rule 必须基于 progress ledger，而不是只基于 node succeeded。

### 7.5 execution_nodes

Execution node 是图中的工作单元。建议字段：`id`、`job_id`、`node_key`、`node_type`、`state`、`title`、`description`、`assignee`、`goal_item_key`、`gap_key`、`human_gate_reason`、`input_summary`、`output_summary`、`constraints_json`、`metadata_json`、`latest_task_id`、`latest_run_id`、`created_at`、`updated_at`、`started_at`、`completed_at`。

`node_key` 是 job 内稳定语义键，用于 patch 引用。不要把自增 id 暴露给 LLM。`node_type` 表示执行意图，不表示 phase。建议初始开放 `analysis`、`implementation`、`verification`、`review`、`debug`、`research`、`human_gate`、`artifact_transform`、`strategy_update`。

### 7.6 execution_dependencies

调度依赖单独成表，只表示 readiness 相关依赖。建议字段：`id`、`job_id`、`from_node_id`、`to_node_id`、`dependency_type`、`required`、`metadata_json`、`created_at`。

`dependency_type` 可为 `depends_on`、`artifact_input`、`requires_verification_target`。这些边参与 DAG 和 readiness 计算。

### 7.7 node_relations

解释性关系不参与 readiness。建议字段：`id`、`job_id`、`from_node_id`、`to_node_id`、`relation_type`、`metadata_json`、`created_at`。

`relation_type` 可为 `verifies`、`supersedes`、`blocks`、`explains`、`derived_from`、`alternative_to`。这样可以避免 verifies、supersedes、blocks 污染调度语义。

### 7.8 node_materializations

一个 node 可能多次物化为 Kanban task/run。不要只在 execution_nodes 上保存单个 task_id/run_id。建议字段：`id`、`job_id`、`node_id`、`task_id`、`run_id`、`attempt`、`lane`、`status`、`created_at`、`started_at`、`completed_at`、`terminal_event_id`、`metadata_json`。

`execution_nodes` 可保留 `latest_task_id` 和 `latest_run_id` 作为快捷引用。

### 7.9 execution_events

结构性事件流，不是日志垃圾桶。建议字段：`id`、`job_id`、`node_id`、`task_id`、`run_id`、`event_type`、`payload_json`、`summary`、`source`、`source_event_id`、`created_at`。

典型 `event_type`：`job_created`、`goal_contract_created`、`goal_item_updated`、`progress_ledger_updated`、`node_created`、`node_materialized`、`node_started`、`node_completed`、`node_failed`、`node_uncertain`、`node_blocked`、`artifact_ready`、`dependency_satisfied`、`decision_requested`、`decision_delta_appended`、`patch_proposed`、`patch_applied`、`patch_rejected`、`human_required`、`human_decision_received`、`liveness_violation`、`stagnation_detected`、`strategy_update_required`、`compaction_requested`、`compaction_completed`、`compaction_failed`。

### 7.10 graph_patches

保存结构变更。建议字段：`id`、`job_id`、`decision_id`、`expected_graph_revision`、`patch_json`、`normalized_patch_json`、`status`、`reject_reason`、`created_at`、`applied_at`。

`status` 可为 `proposed`、`applied`、`rejected`、`noop`、`stale`。

### 7.11 kernel_decisions

保存每次决策调用。建议字段：`id`、`job_id`、`segment_id`、`trigger_event_id`、`db_revision`、`graph_revision`、`ledger_revision`、`input_delta_json`、`provider_request_ref`、`provider_response_json`、`parsed_patch_json`、`model`、`provider`、`status`、`error`、`created_at`、`completed_at`。

`input_delta_json` 是追加到 decision session 的增量，不是完整冷启动 snapshot。

### 7.12 decision_session_segments

Decision session segment 是系统级调度对话的活跃片段。建议字段：`id`、`job_id`、`session_id`、`segment_index`、`state`、`started_at`、`closed_at`、`start_decision_id`、`end_decision_id`、`covered_event_start_id`、`covered_event_end_id`、`covered_graph_revision_start`、`covered_graph_revision_end`、`estimated_input_tokens`、`estimated_output_tokens`、`active_segment_tokens`、`compacted_checkpoint_id`、`archive_ref`、`metadata_json`。

`state` 可为 `active`、`archived`、`compacted`、`failed_compaction`。

### 7.13 decision_segment_entries

第一版就应建立一等 append-only entries 表，而不是从多张表反推 transcript。字段：`id`、`job_id`、`segment_id`、`entry_index`、`entry_type`、`ref_type`、`ref_id`、`payload_json`、`payload_text`、`estimated_tokens`、`created_at`。

`entry_type` 可为 `delta`、`provider_input`、`provider_raw_output`、`parsed_patch`、`validator_result`、`patch_applied`、`patch_rejected`、`graph_gap_change`、`human_decision`、`compaction_event`。

这张表定义 active session 真实追加顺序，是 compaction 的直接输入。

### 7.14 decision_checkpoints

Checkpoint 是压缩后的下一阶段决策前缀。字段：`id`、`job_id`、`source_segment_id`、`profile_name`、`profile_version`、`profile_hash`、`checkpoint_revision`、`db_revision`、`graph_revision`、`ledger_revision`、`covered_event_range_json`、`covered_decision_range_json`、`payload_json`、`payload_text`、`validator_status`、`reject_reason`、`created_at`、`supersedes_checkpoint_id`、`metadata_json`。

checkpoint payload 必须带 provenance。每个关键 conclusion 应引用 `event_id`、`decision_id`、`patch_id`、`ledger_entry_id`、`node_key`、`goal_item_key`、`artifact_ref` 或 `human_decision_id`。

### 7.15 node_artifacts

保存节点产物引用。字段：`id`、`job_id`、`node_id`、`artifact_type`、`path_or_ref`、`summary`、`hash`、`size`、`metadata_json`、`created_at`。

Artifact 可引用 worker evidence、代码 diff、测试结果、报告、外部工具输出等。

## 8. Goal Contract 设计

创建 runtime job 时，系统应创建初始 goal contract。第一版可以由 deterministic builder 根据 objective 创建粗粒度 goal items；后续可由 decision provider 辅助生成，但必须经过本地 schema 和人工可解释约束。

Goal contract 不应是完整计划，而是完成条件。

示例：

```json
{
  "objective_summary": "Implement a stock backtesting system with market data input and a runnable basic strategy.",
  "items": [
    {
      "goal_key": "runnable_entrypoint",
      "title": "Runnable entrypoint exists",
      "required": true,
      "verification_required": true
    },
    {
      "goal_key": "data_provider",
      "title": "Data provider abstraction exists",
      "required": true,
      "verification_required": true
    },
    {
      "goal_key": "basic_strategy",
      "title": "At least one basic strategy can run",
      "required": true,
      "verification_required": true
    },
    {
      "goal_key": "backtest_output",
      "title": "Backtest produces inspectable result output",
      "required": true,
      "verification_required": true
    },
    {
      "goal_key": "usage_doc",
      "title": "Usage instructions are documented",
      "required": true,
      "verification_required": false
    }
  ],
  "human_gate_policy": {
    "requires_user_for": [
      "paid_external_api",
      "credentials",
      "destructive_migration",
      "high_impact_architecture_choice"
    ],
    "default_allow": [
      "internal_file_layout",
      "mock_first_strategy",
      "test_file_creation"
    ]
  }
}
```

Job completion 必须基于 required goal items 是否有 sufficient evidence，而不是 graph 是否跑完。

## 9. Progress Ledger 设计

worker evidence ingest 后，runtime 应更新 progress ledger。

一个 node succeeded 只代表局部执行成功，不代表某个 goal item 自动 satisfied。必须通过 receipt、artifact、verifier 或 evaluator 映射到 goal item。

Ledger entry 应区分 `self_reported`、`partial`、`verified`、`failed` 和 `waived`。Completion rule 默认只接受 verified 或明确允许的 self_reported。

## 10. Gap Detector

Gap detector 从 goal contract 和 progress ledger 推导 open gaps。

典型 gap 包括 required goal item unmet、goal item partial but missing verifier、goal item blocked by external permission、node succeeded but no evidence linked to required goal、verifier failed、implementation exists but no usage path、all graph frontier exhausted but goal incomplete。

Gap detector 输出 `gap_key`，并可触发 `decision_requested`。

## 11. Liveness Invariant

Runtime supervisor 必须维护：如果 job 未 done 且未 cancelled/failed，则必须至少满足以下之一：存在 running node；存在 ready node；存在 active human gate；存在 pending decision；存在 scheduled compaction/decision；即将触发 gap decision。

否则记录 `liveness_violation`，并触发 goal-gap decision 或 strategy update。

## 12. Decision Session

### 12.1 作用

Decision session 是 job 级长期推理上下文。它服务于 LLM decision provider 的连续理解和 prefix cache。它不是事实源，不拥有写权限，不能直接影响 DB。

### 12.2 输入组织

Provider input 由以下部分组成：stable runtime contract；current goal contract；latest validated checkpoint；short tail；current delta。

不要每轮从 DB 重拼不同顺序的巨大 snapshot。动态内容放在尾部，稳定前缀尽量不变。

### 12.3 Delta

Delta 是当前 DB state 变化在本轮决策中的投影。示例字段：

```json
{
  "trigger_reason": "goal_gap_no_ready_node",
  "db_revision": 42,
  "graph_revision": 17,
  "ledger_revision": 9,
  "recent_events": [
    {
      "event_type": "node_failed",
      "node_key": "verify-provider-contract",
      "summary": "Verifier failed because timestamp field type mismatched."
    }
  ],
  "open_gaps": [
    {
      "gap_key": "end_to_end_backtest_unverified",
      "goal_item_key": "backtest_output",
      "reason": "No verified end-to-end run evidence."
    }
  ],
  "available_patch_ops": [
    "create_node",
    "add_dependency",
    "insert_verifier",
    "split_node",
    "request_human",
    "supersede_node",
    "strategy_update"
  ]
}
```

## 13. Decision Session Compaction Runtime

### 13.1 压缩对象

压缩对象是 job 级 decision session transcript，也就是 DB-derived delta、provider patch、validator result、patch applied/rejected、graph/gap/ledger state change、human decision 和 strategy update。

压缩对象不是 worker log，不是 execution log，不是 worker receipt。

### 13.2 Segment 生命周期

每个 job 有 active decision session segment。

每次 decision 调用会向 active segment 追加 entries。达到 compaction 条件后：关闭 active segment；调用 compaction provider；生成 checkpoint candidate；validator 校验 checkpoint；checkpoint 通过后归档旧 segment；开启新 active segment；新 segment 使用 stable contract + goal contract + checkpoint + short tail + new delta；旧 segment 原文不再进入 active LLM context。

### 13.3 Checkpoint 语义

Checkpoint 是下一阶段的决策前缀，不是事实源。

Checkpoint 应包含 objective_summary、goal_contract_revision、active_milestone、satisfied_goal_items、open_goal_gaps、open_blockers、key_decisions、rejected_approaches、known_failure_boundaries、validator_rejection_lessons、human_decisions、artifact_index、graph_frontier、do_not_repeat 和 next_strategy_constraints。

每个重要结论应带 provenance。

### 13.4 Short Tail

Short tail 只能包含 checkpoint 之后尚未被覆盖的少量 entries。必须同时受 entry count 和 token budget 限制。

旧 segment 原文不得通过 tail 大量回流。否则 compaction 失效。

### 13.5 Compaction Policy

Compaction 触发由可配置 policy 决定，不写死在 kernel。Policy 输入包括 active_segment_tokens、stable_prefix_tokens、checkpoint_tokens、tail_tokens、delta_tokens、model_output_tokens、cacheable_prefix_tokens、context_window_ratio、accepted_patch_count、rejected_patch_count、noop_count、growth_rate、milestone_transition、human_decision_changed_goal、validator_rejection_streak 和 anti_stuck_signal。

第一版建议支持 manual 和 token threshold。后续再加语义触发。

### 13.6 Compaction Profile

压缩提示词应热插拔为 Markdown profile。建议目录：

```text
docs/runtime_compaction_profiles/
  token_budget_compaction.md
  validator_boundary_compaction.md
  human_decision_compaction.md
  milestone_transition_compaction.md
  anti_stuck_compaction.md
```

每个 profile 应包含用途、输入选择规则、压缩目标、禁止事项、输出 schema、validator 要求、示例、profile version/hash。checkpoint row 必须记录 `profile_name`、`profile_version` 或 `profile_hash`。

### 13.7 Compaction Provider

接口：

```python
compaction_provider(segment, db_state, profile, budget) -> checkpoint_candidate
```

第一版可实现 deterministic DB-derived checkpoint，不接真实 LLM。其目的只是证明 segment close、checkpoint generation、checkpoint validation、new segment open、old transcript exclusion 的生命周期成立。

真实 LLM compaction provider 后续接入。

### 13.8 Checkpoint Validator

checkpoint validator 检查引用的 `node_key`、`goal_item_key`、`artifact_ref`、`patch_id`、`event_id`、`decision_id`、`human_decision_id` 是否存在；未验证事项未被写成 confirmed；failed verifier 未被写成 passed；partial evidence 未被写成 satisfied；hard blocker 未被遗漏；db/graph/ledger revision 未冲突。

## 14. Graph Patch

### 14.1 允许 op

第一阶段建议支持 `create_node`、`add_dependency`、`insert_verifier`、`split_node`、`supersede_node`、`request_human`、`strategy_update`、`noop`。

不建议开放 `release_node`。readiness 由本地 reducer 计算。

`complete_job` 不应开放给 LLM，或只允许作为 suggestion，由 validator 基于 progress ledger 接受/拒绝。

`mark_blocked` 应严格限制，建议改为 `request_blocked_state`，由本地规则确认。

### 14.2 create_node

必须包含 `node_key`、`node_type`、`title`、`description`、`goal_item_key` 或 `gap_key` 或 `human_gate_reason`、`assignee` 或 lane selection policy、`constraints`、`depends_on`。

无目标关联的 `create_node` 应拒绝。

### 14.3 request_human

必须包含 `decision_type`、`question`、`context_summary`、`options`、`default_recommendation`、`why_user_required`、`risk_if_defaulted`、`affected_goal_items`。

`request_human` 必须通过 human gate policy 检查。

### 14.4 expected_revision

所有 patch 必须带 `expected_graph_revision`。应用时若当前 `graph_revision` 不匹配，patch 标为 `stale` 或重新 validate。

## 15. Patch Validator

Validator 是本地安全边界。

必须拒绝未知 op、未知 node_key、重复 node_key、空 title/description、未知 node_type、无 goal/gap/human linkage、创建环、非法状态迁移、修改 terminal fact、绕过 verifier、`release_node`、直接 `complete_job`、无合法 blocker_type 的 blocked、assignee/lane 不存在且策略严格、patch 超过 op 上限、patch 超过大小上限、expected_revision 过期、引用不存在 artifact、引用不存在 goal item。

部分幂等可以视为 noop，例如重复 `add_dependency` 且字段一致。

## 16. Worker Execution Contract

worker task body 应包含 `runtime_job_id`、`execution_node_id`、`node_key`、`node_type`、`goal_item_key` 或 `gap_key`、root objective summary、node title/description、constraints、dependency outputs、artifact refs、success evidence contract、verification expectations、human gate boundary、receipt schema。

worker receipt 是节点交付契约，不是 runtime compaction。

receipt 应返回 `verdict`、`summary`、`changed_files`、`commands_run`、`verification_results`、`artifact_refs`、`failure_reason`、`risk_notes`、`claimed_goal_items`、`partial_goal_items`、`unmet_goal_items`、`new_constraints`、`human_required`、`blocked_reason`。

worker 内部 Codex/CC 可以自行压缩上下文，runtime 不管理其内部压缩。

## 17. Evidence Ingest

ingest 读取 Kanban `task_progress_snapshot` 和 artifact refs，规范化为 node state、execution event、artifact refs、progress ledger updates、goal gap changes 和 decision session delta entries。

ingest 必须幂等，防止同一 run terminal event 重复写入。

verdict 映射：`pass`、`success`、`approved` -> `succeeded`；`needs_fix`、`failed`、`error` -> `failed`；`blocked` -> `blocked`；`human_required` -> `waiting_human`；`uncertain`、`unknown` -> `node_uncertain`。

## 18. Readiness Reducer

readiness 由本地 reducer 计算。

输入包括 execution_dependencies、node states、resource policy、human gate 和 job state。输出包括 `planned -> waiting_dependency`、`waiting_dependency -> ready`、ready node 可物化、running node 等待 worker、failed node 等待 strategy/debug/supersede、succeeded node 不可被改写。

LLM 不得 release node。

## 19. Supervisor

Supervisor 可以工程上 bounded，但逻辑上 goal-driven。

每次 tick：ingest worker evidence；update node states；update progress ledger；detect goal gaps；update readiness；materialize ready nodes；if decision needed, append delta and call provider；apply/reject patch；if compaction needed, run compaction lifecycle；check completion；check liveness。

合法退出：done、waiting_worker、waiting_human、waiting_decision/provider unavailable、budget exhausted but resumable、cancelled、system error requiring operator。

不合法退出：goal incomplete、no ready、no running、no human gate、no pending decision、no gap decision scheduled。此时应记录 `liveness_violation`。

## 20. Human Gate Policy

Human gate 只用于真实需要用户判断的情况。

允许 human gate 的情况：外部费用、credentials/secrets、权限、破坏性迁移、产品偏好、高影响架构路线、多个合理方案且无默认依据。

不应 human gate 的情况：内部目录结构、函数命名、普通测试创建、mock-first 实现、非破坏性 refactor、worker 局部实现细节。

## 21. Anti-Stuck Policy

Stuck signals 包括同一 gap 多轮无新增 evidence、同类 node 连续失败、decision provider 连续 noop、patch 连续 rejected、worker 多次 uncertain、milestone 超预算、所有节点 succeeded 但 progress ledger 无推进。

触发后产生 synthetic event：`stagnation_detected`、`strategy_update_required`。

strategy update 不应重复创建同类节点。应要求改变策略：拆小、换 lane、插入 research、插入 verifier、调整 milestone、request human、supersede 失败路径、降低当前 slice。

## 22. API 初稿

建议 API：

```python
create_runtime_job(conn, root_task_id, objective, board=None)
status_runtime_job(conn, job_id)
advance_runtime_job(conn, job_id, *, board=None, decision_provider=None, max_patches=1)
supervise_runtime_job(conn, job_id, *, mode="once", max_steps=None, interval=5.0)
apply_graph_patch(conn, job_id, patch, decision_id=None)
build_decision_delta(conn, job_id, trigger_event_id=None)
append_decision_entry(conn, segment_id, entry)
compact_decision_session(conn, job_id, profile=None, reason=None)
build_provider_request(conn, job_id, delta)
ingest_runtime_node_evidence(conn, node_id, board=None)
create_human_decision(conn, job_id, gate_node_key, decision_payload)
```

Dashboard API 后续：`/runtime/jobs`、`/runtime/jobs/{id}`、`/runtime/jobs/{id}/advance`、`/runtime/jobs/{id}/graph`、`/runtime/jobs/{id}/goals`、`/runtime/jobs/{id}/ledger`、`/runtime/jobs/{id}/events`、`/runtime/jobs/{id}/patches`、`/runtime/jobs/{id}/decision-session`、`/runtime/jobs/{id}/compact`、`/runtime/jobs/{id}/human-decision`。

## 23. 实现阶段

Phase 1: Goal-driven Graph Kernel 基础闭环。实现 schema、create_runtime_job、初始 goal contract / goal items / progress ledger、initial understanding node、node materialization、evidence ingest、deterministic decision provider、patch validator、goal-driven completion、analysis -> implementation -> verification -> done fixture。注意：这个 fixture 只是测试路径，不是默认 workflow。

Phase 2A: Runtime Control Plane / Kanban Substrate Wiring。新增薄 CLI/API 入口、runtime status/advance helpers、existing Kanban dispatcher/worker-lane fixture 联通、node -> Kanban task -> worker evidence -> runtime ingest 的可验证链路。不要在这一阶段接真实 LLM、daemon 或 dashboard UI。

Phase 2B: Decision Provider / Decision Session Foundation。新增 decision session、segment、decision delta append、provider request composition、kernel_decisions input_delta_json、record/replay provider、strict provider-output parsing、patch rejection feedback to session、prefix-cache-friendly layout。

Phase 2C: Goal Progression Hardening。补硬 progress ledger、goal gap detector、goal-driven completion、node 必须关联 goal/gap、ledger-aware worker receipt ingest、liveness reducer、synthetic events、anti-stuck policy、human gate policy、strategy_update op、合法/非法 supervisor exit 检查。

Phase 2D: Decision Session Compaction Runtime。新增 decision_session_segments、decision_segment_entries、decision_checkpoints、deterministic compaction provider、checkpoint validator、profile md loader、manual/token compaction policy、provider input excluding old transcript、tests for segment replacement。

Phase 3: Real LLM Providers。接入真实 decision provider、真实 compaction provider、JSON/schema robust parsing、provider retry、model/profile selection、prefix cache telemetry。

Phase 4: Dashboard。展示 goal contract、progress ledger、execution graph、active frontier、events、patch history、decision session segment/checkpoint、compaction status、human gate、worker evidence、liveness/anti-stuck signals。

## 24. 测试策略

第一批测试全部 deterministic，不依赖外部 LLM 或网络。

必须覆盖 schema init、create job、goal contract creation、initial node、patch validator、cycle rejection、unknown node rejection、node without goal/gap rejection、ready computation、Kanban materialization、evidence ingest、progress ledger update、goal gap detection、decision delta append、patch apply/reject、expected_revision stale rejection、job completion from ledger、liveness violation、human gate policy、anti-stuck signal、segment entry append、manual compaction、checkpoint validation、old transcript excluded from provider input、checkpoint provenance validation、profile hash recorded、new segment created after compaction、worker receipt not treated as compaction、node materialization retry history。

第二批接 existing worker lane fixtures，验证 node -> Kanban task -> worker evidence -> runtime ingest。

真实 Codex/CC smoke 作为集成测试，不作为单测前提。

## 25. 关键不变量

DB 是事实源。

Decision session 不是事实源。

旧 segment 被压缩后不得进入 active provider input。

Checkpoint 必须绑定 DB/graph/ledger revision。

Checkpoint 关键结论必须可追溯。

LLM patch 只能 proposal。

Validator 是唯一落库边界。

readiness 本地计算。

completion 本地计算。

blocked 本地确认。

node 必须服务 goal/gap/human reason。

goal 未完成不得静默停止。

worker receipt 是交付契约，不是 compaction。

worker 内部压缩属于 backend。

decision session compaction 只处理系统级调度对话。

## 26. 开放问题

Goal contract 第一版由 deterministic builder 生成，还是允许 LLM 初始建议后本地确认，需要进一步选择。

Progress ledger 的 satisfaction_rule 是完全本地规则，还是允许轻量 evaluator 辅助，需要根据第一批任务复杂度决定。

Checkpoint payload 是否同时保存 JSON 和 text，推荐保存两者，但第一版可以先 JSON + rendered text。

Decision segment entries 是否第一版就建表，强烈建议建表。

Human gate policy 的默认规则需要从真实任务中调整。

Compaction profile 的 schema 需要在实现时稳定下来，避免 profile 只是 prompt 而没有 validator 语义。

真实 provider 的 session_ref 是否使用 API 侧 conversation/thread id，还是本地 transcript 渲染，需要根据具体模型 API 决定。

## 27. 总结

Hermes Kanban Runtime Kernel v2 的核心不是多 agent，而是 goal-driven runtime。

Kanban 负责执行，worker 负责局部节点，DB 负责事实，goal contract 定义目标，progress ledger 证明进展，execution graph 承载当前工作结构，decision session 提供推理连续性，decision session compaction 管理长期上下文，validator 保护系统边界，supervisor 维持 liveness。

这个架构保留 Codex/CC loop 的关键优势，即长上下文、局部执行闭环、上下文压缩和工具反馈，同时避免把模型上下文当作事实源。它也避免旧 Orchestra 的 role workflow 和 fixed phase manager loop，让复杂任务可以围绕目标证据持续推进，而不是围绕预设角色流程运行。
